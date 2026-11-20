"""Regression tests for multi-node ComfyUI concurrent pool, endpoint persistence, failover, and workbench integration."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from h3ref import colab_runtime, comfy, config, film
from scripts import start_colab
from workbench import server


def _make_png(path: Path, color=(40, 80, 120)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, format="PNG")
    return path


def _build_three_segment_job(root: Path) -> tuple[film.Job, dict, list[dict], dict]:
    job_dir = root / "job"
    (job_dir / "frames").mkdir(parents=True, exist_ok=True)
    (job_dir / "clips").mkdir(parents=True, exist_ok=True)
    cast_img = _make_png(root / "cast.png", (120, 60, 40))
    film_obj = {
        "aspect": "9:16",
        "cast": [{"name": "A", "look": "tall", "aliases": [], "picture": str(cast_img)}],
        "shots": [
            {"start": 0.0, "end": 12.0, "action": "A walks", "camera": "wide", "line": ""},
            {"start": 12.0, "end": 24.0, "action": "A talks", "camera": "close", "line": ""},
            {"start": 24.0, "end": 36.0, "action": "A leaves", "camera": "wide", "line": ""},
        ],
    }
    segs = film.plan(film_obj["shots"])
    assert len(segs) == 3
    pics = {s["idx"]: film.refs(film_obj, s, "shot") for s in segs}
    for s in segs:
        _make_png(job_dir / "frames" / f"s{s['idx'] + 1}-1.png", (10 * (s["idx"] + 1), 90, 140))
    job = film.Job(job_dir)
    for s in segs:
        job.put("segs", str(s["idx"]), zh=f"seg{s['idx'] + 1}", h3=f"prompt seg{s['idx'] + 1}")
    return job, film_obj, segs, pics


class ComfyMultiNodeConfigTest(unittest.TestCase):
    def test_multi_url_parsing_dedup_concurrency_auto_and_key_requirement(self):
        settings = config.video_settings({
            "VIDEO_PROVIDER": "comfyui",
            "COMFYUI_URL": "https://node1.ngrok-free.dev/\nhttps://node2.example.com, https://node1.ngrok-free.dev",
            "COMFYUI_API_KEY": "shared-secret-key",
        })
        self.assertEqual(
            settings["comfyui_urls"],
            ["https://node1.ngrok-free.dev", "https://node2.example.com"],
        )
        self.assertEqual(settings["comfyui_url"], "https://node1.ngrok-free.dev")
        self.assertEqual(settings["comfyui_urls_text"], "https://node1.ngrok-free.dev,https://node2.example.com")
        self.assertTrue(settings["comfy_requires_key"])
        self.assertEqual(settings["comfyui_concurrency"], 2)
        self.assertFalse(settings["comfyui_concurrency_explicit"])
        self.assertEqual(settings["concurrency"], 2)

        explicit = config.video_settings({
            "VIDEO_PROVIDER": "comfyui",
            "COMFYUI_URL": "http://127.0.0.1:8188, http://localhost:8189",
            "COMFYUI_CONCURRENCY": "1",
        })
        self.assertFalse(explicit["comfy_requires_key"])
        self.assertEqual(explicit["comfyui_concurrency"], 1)
        self.assertTrue(explicit["comfyui_concurrency_explicit"])

        mixed = config.video_settings({
            "VIDEO_PROVIDER": "comfyui",
            "COMFYUI_URL": "http://127.0.0.1:8188, https://remote.example.com",
        })
        self.assertTrue(mixed["comfy_requires_key"])
        self.assertEqual(mixed["comfyui_concurrency"], 2)

        with self.assertRaises(ValueError) as ctx:
            config.video_settings({
                "COMFYUI_URL": "https://node1.example.com, https://user:secret_leak@node2.example.com",
            })
        self.assertNotIn("secret_leak", str(ctx.exception))


class ComfyMultiNodePoolExecutionTest(unittest.TestCase):
    def setUp(self):
        comfy.clear_upload_cache()

    def tearDown(self):
        comfy.clear_upload_cache()

    def test_upload_cache_isolated_by_endpoint_url_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = _make_png(Path(tmp) / "ref.png")
            calls: list[str] = []

            def fake_upload(url, key, path, fields, **kw):
                calls.append(url)
                return {"name": kw["filename"], "subfolder": ""}

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://node-a.example.com,https://node-b.example.com",
                "COMFYUI_API_KEY": "shared-key",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(comfy.http, "upload", side_effect=fake_upload):
                n1 = comfy.upload(img, base_url="https://node-a.example.com")
                n2 = comfy.upload(img, base_url="https://node-a.example.com")
                n3 = comfy.upload(img, base_url="https://node-b.example.com")
                n4 = comfy.upload(img, base_url="https://node-b.example.com")
                self.assertEqual(n1, n2)
                self.assertEqual(n1, n3)
                self.assertEqual(n3, n4)
                self.assertEqual(
                    calls,
                    [
                        "https://node-a.example.com/upload/image",
                        "https://node-b.example.com/upload/image",
                    ],
                )
                # Clearing node-a cache forces re-upload only on node-a, leaving node-b cached
                comfy.clear_upload_cache("https://node-a.example.com")
                comfy.upload(img, base_url="https://node-a.example.com")
                comfy.upload(img, base_url="https://node-b.example.com")
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[-1], "https://node-a.example.com/upload/image")

    def test_parallel_dispatch_across_two_nodes_and_persists_per_segment_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            job, film_obj, segs, pics = _build_three_segment_job(Path(tmp))
            node_a = "https://node-a.example.com"
            node_b = "https://node-b.example.com"
            lock = threading.Lock()
            active = 0
            max_active = 0
            submitted_to: dict[str, str] = {}
            polled_on: dict[str, str] = {}
            barrier = threading.Barrier(2, timeout=5.0)
            first_two_hit = [0]

            def fake_probe(url, api_key=None, timeout=12.0):
                return {"url": url, "label": comfy.endpoint_label(url), "online": True, "auth_failed": False, "queue_running": 0, "queue_pending": 0, "devices": []}

            def fake_upload_many(paths, *, max_workers=4, base_url=None, api_key=None):
                return [f"hash_{idx}.png" for idx, _ in enumerate(paths)]

            def fake_submit(g, log, *, record_submission=None, base_url=None, api_key=None):
                nonlocal active, max_active
                if record_submission:
                    record_submission(True)
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    hit_idx = first_two_hit[0]
                    first_two_hit[0] += 1
                    pid = f"pid-{len(submitted_to) + 1}"
                    submitted_to[pid] = base_url or ""
                if hit_idx < 2:
                    barrier.wait()
                return pid

            def fake_poll(task, *, base_url=None, api_key=None):
                nonlocal active
                with lock:
                    polled_on[task] = base_url or ""
                    active = max(0, active - 1)
                return {"state": "done", "url": f"{base_url}/view?filename={task}.mp4", "cny": 0.0, "coins": 0.0, "run_seconds": 10.0}

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"{node_a},{node_b}",
                "COMFYUI_API_KEY": "shared-secret",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(comfy, "probe_endpoint", side_effect=fake_probe), \
                 mock.patch.object(comfy, "upload_many", side_effect=fake_upload_many), \
                 mock.patch.object(comfy, "submit_with_retry", side_effect=fake_submit), \
                 mock.patch.object(comfy, "poll", side_effect=fake_poll), \
                 mock.patch.object(comfy, "download", side_effect=lambda u, out: out.write_bytes(b"mp4") or out), \
                 mock.patch.object(film.media, "last_frame", side_effect=lambda src, dst: dst.write_bytes(b"jpg")):
                film.stage_generate(job, film_obj, segs, pics, "shot")

            self.assertEqual(max_active, 2, "Independent segments must execute concurrently across both nodes")
            self.assertEqual(set(submitted_to.values()), {node_a, node_b})
            for pid, ep in submitted_to.items():
                self.assertEqual(polled_on[pid], ep, "Each task must be polled on the exact node it was submitted to")
            saved_urls = [job.state["segs"][str(i)]["comfyui_url"] for i in range(3)]
            self.assertTrue(all(u in (node_a, node_b) for u in saved_urls))

    def test_resume_routes_inflight_segment_to_persisted_comfyui_url_even_when_config_reordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            job, film_obj, segs, pics = _build_three_segment_job(Path(tmp))
            segs = segs[:2]
            node_a = "https://node-a.example.com"
            node_b = "https://node-b.example.com"
            node_c = "https://node-c.example.com"
            # seg1 was previously submitted to node_b (which is no longer even in COMFYUI_URL!)
            job.put("segs", "0", provider="comfyui", comfyui_url=node_b, task="pid-on-b", seed=123, submitted=time.time())

            poll_calls: list[tuple[str, str | None]] = []
            download_urls: list[str] = []
            submit_nodes: list[str | None] = []

            def fake_probe(url, api_key=None, timeout=12.0):
                return {"url": url, "label": comfy.endpoint_label(url), "online": True, "auth_failed": False, "queue_running": 0, "queue_pending": 0, "devices": []}

            def fake_submit(g, log, *, record_submission=None, base_url=None, api_key=None):
                if record_submission:
                    record_submission(True)
                submit_nodes.append(base_url)
                return "pid-new-2"

            def fake_poll(task, *, base_url=None, api_key=None):
                poll_calls.append((task, base_url))
                return {"state": "done", "url": f"{base_url}/view?filename={task}.mp4", "cny": 0.0, "coins": 0.0, "run_seconds": 8.0}

            def fake_download(url, out):
                download_urls.append(url)
                out.write_bytes(b"mp4")
                return out

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"{node_a},{node_c}",
                "COMFYUI_API_KEY": "shared-secret",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(comfy, "probe_endpoint", side_effect=fake_probe), \
                 mock.patch.object(comfy, "upload_many", return_value=["a.png", "b.png"]), \
                 mock.patch.object(comfy, "submit_with_retry", side_effect=fake_submit), \
                 mock.patch.object(comfy, "poll", side_effect=fake_poll), \
                 mock.patch.object(comfy, "download", side_effect=fake_download), \
                 mock.patch.object(film.media, "last_frame", side_effect=lambda src, dst: dst.write_bytes(b"jpg")):
                film.stage_generate(job, film_obj, segs, pics, "shot")

            self.assertIn(("pid-on-b", node_b), poll_calls)
            self.assertIn(f"{node_b}/view?filename=pid-on-b.mp4", download_urls)
            self.assertEqual(len(submit_nodes), 1)
            self.assertIn(submit_nodes[0], (node_a, node_c))
            self.assertEqual(job.state["segs"]["0"]["comfyui_url"], node_b)
            self.assertEqual(job.state["segs"]["1"]["comfyui_url"], submit_nodes[0])

    def test_upload_failure_isolates_node_and_fails_over_without_duplicate_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            job, film_obj, segs, pics = _build_three_segment_job(Path(tmp))
            segs = segs[:2]
            node_a = "https://node-a.example.com"
            node_b = "https://node-b.example.com"
            upload_attempts: list[str | None] = []
            submit_nodes: list[str | None] = []

            def fake_probe(url, api_key=None, timeout=12.0):
                return {"url": url, "label": comfy.endpoint_label(url), "online": True, "auth_failed": False, "queue_running": 0, "queue_pending": 0, "devices": []}

            def fake_upload_many(paths, *, max_workers=4, base_url=None, api_key=None):
                upload_attempts.append(base_url)
                if base_url == node_a:
                    raise RuntimeError("ConnectionResetError: node-a dropped during upload")
                return ["ref1.png", "ref2.png"]

            def fake_submit(g, log, *, record_submission=None, base_url=None, api_key=None):
                if record_submission:
                    record_submission(True)
                submit_nodes.append(base_url)
                return f"pid-{len(submit_nodes)}"

            def fake_poll(task, *, base_url=None, api_key=None):
                return {"state": "done", "url": f"{base_url}/view?filename={task}.mp4", "cny": 0.0, "coins": 0.0, "run_seconds": 9.0}

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"{node_a},{node_b}",
                "COMFYUI_API_KEY": "shared-secret",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(comfy, "probe_endpoint", side_effect=fake_probe), \
                 mock.patch.object(comfy, "upload_many", side_effect=fake_upload_many), \
                 mock.patch.object(comfy, "submit_with_retry", side_effect=fake_submit), \
                 mock.patch.object(comfy, "poll", side_effect=fake_poll), \
                 mock.patch.object(comfy, "download", side_effect=lambda u, out: out.write_bytes(b"mp4") or out), \
                 mock.patch.object(film.media, "last_frame", side_effect=lambda src, dst: dst.write_bytes(b"jpg")):
                film.stage_generate(job, film_obj, segs, pics, "shot")

            # node_a failed during upload -> isolated, never submitted to; both segments completed on node_b
            self.assertEqual(submit_nodes, [node_b, node_b])
            self.assertEqual(upload_attempts.count(node_a), 1, "Isolated node must not be retried by subsequent segments")
            self.assertEqual(job.state["segs"]["0"]["comfyui_url"], node_b)
            self.assertEqual(job.state["segs"]["1"]["comfyui_url"], node_b)

    def test_ambiguous_submit_timeout_sets_submission_unknown_and_never_fails_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            job, film_obj, segs, pics = _build_three_segment_job(Path(tmp))
            segs = segs[:1]
            node_a = "https://node-a.example.com"
            node_b = "https://node-b.example.com"
            submit_calls: list[str | None] = []

            def fake_probe(url, api_key=None, timeout=12.0):
                return {"url": url, "label": comfy.endpoint_label(url), "online": True, "auth_failed": False, "queue_running": 0, "queue_pending": 0, "devices": []}

            def fake_submit_timeout(g, *, base_url=None, api_key=None):
                submit_calls.append(base_url)
                raise RuntimeError("ReadTimeout after POST /prompt")

            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"{node_a},{node_b}",
                "COMFYUI_API_KEY": "shared-secret",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(comfy, "probe_endpoint", side_effect=fake_probe), \
                 mock.patch.object(comfy, "upload_many", return_value=["ref1.png", "ref2.png"]), \
                 mock.patch.object(comfy, "submit", side_effect=fake_submit_timeout):
                with self.assertRaises(RuntimeError):
                    film.stage_generate(job, film_obj, segs, pics, "shot")

            self.assertEqual(len(submit_calls), 1, "Ambiguous /prompt failure must NEVER fail over or resubmit")
            seg_rec = job.state["segs"]["0"]
            self.assertTrue(seg_rec.get("submission_unknown"))
            self.assertEqual(seg_rec.get("comfyui_url"), submit_calls[0])

    def test_partial_offline_probe_degrades_gracefully_and_workbench_allows_launch(self):
        node_a = "https://node-a.example.com"
        node_b = "https://node-b.example.com"
        env = {
            "VIDEO_PROVIDER": "comfyui",
            "COMFYUI_URL": f"{node_a},{node_b}",
            "COMFYUI_API_KEY": "shared-secret",
        }

        def fake_probe_json(url, key, *, timeout=12.0):
            if url.startswith(node_a):
                return None, None
            if url.endswith("/system_stats"):
                return 200, {"system": {"comfyui_version": "0.3.1"}, "devices": [{"name": "RTX PRO 6000"}]}
            return 200, {"queue_running": [["1", "t1"]], "queue_pending": []}

        with mock.patch.object(colab_runtime, "_probe_json", side_effect=fake_probe_json):
            probe = colab_runtime.probe_comfyui(env)
            self.assertTrue(probe["online"])
            self.assertEqual(probe["online_count"], 1)
            self.assertEqual(probe["total_count"], 2)
            self.assertEqual(len(probe["nodes"]), 2)
            self.assertFalse(probe["nodes"][0]["online"])
            self.assertTrue(probe["nodes"][1]["online"])
            self.assertEqual(probe["nodes"][1]["queue_running"], 1)
            self.assertEqual(colab_runtime.probe_remote_queue_count(env), 1)

    def test_workbench_config_multi_url_newline_normalization_key_retention_and_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            app = server.create_app(storage=root / ".workbench", env_path=env_path)
            client = app.test_client()
            headers = {"X-Workbench": "1"}

            res = client.post(
                "/api/config",
                headers=headers,
                json={
                    "VIDEO_PROVIDER": "comfyui",
                    "COMFYUI_URL": "https://node-a.ngrok-free.dev/",
                    "COMFYUI_API_KEY": "shared-pool-key-1234",
                    "persist": True,
                },
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.get_json()["keys"]["COMFYUI_API_KEY"])
            self.assertEqual(res.get_json()["effective_comfyui_concurrency"], 1)

            # Expanding [A] -> [A, B] with newline separator normalizes to comma line in .env and keeps shared key
            res2 = client.post(
                "/api/config",
                headers=headers,
                json={
                    "COMFYUI_URL": "https://node-a.ngrok-free.dev/\nhttps://node-b.example.com/",
                    "COMFYUI_CONCURRENCY": "2",
                    "persist": True,
                },
            )
            self.assertEqual(res2.status_code, 200)
            body2 = res2.get_json()
            self.assertEqual(body2["COMFYUI_URL"], "https://node-a.ngrok-free.dev,https://node-b.example.com")
            self.assertEqual(body2["COMFYUI_URLS"], ["https://node-a.ngrok-free.dev", "https://node-b.example.com"])
            self.assertEqual(body2["COMFYUI_CONCURRENCY"], "2")
            self.assertEqual(body2["effective_comfyui_concurrency"], 2)
            self.assertTrue(body2["keys"]["COMFYUI_API_KEY"])
            disk_env = config.read_env_file(env_path)
            self.assertEqual(disk_env["COMFYUI_URL"], "https://node-a.ngrok-free.dev,https://node-b.example.com")
            self.assertEqual(disk_env["COMFYUI_CONCURRENCY"], "2")

            # Clearing COMFYUI_CONCURRENCY reverts to auto (matching node count = 2)
            res3 = client.post(
                "/api/config",
                headers=headers,
                json={"COMFYUI_CONCURRENCY": "", "persist": True},
            )
            self.assertEqual(res3.status_code, 200)
            self.assertEqual(res3.get_json()["COMFYUI_CONCURRENCY"], "")
            self.assertEqual(res3.get_json()["effective_comfyui_concurrency"], 2)
            self.assertNotIn("COMFYUI_CONCURRENCY", config.read_env_file(env_path))

            # Replacing with completely disjoint node [C] without supplying key clears stored key
            res4 = client.post(
                "/api/config",
                headers=headers,
                json={"COMFYUI_URL": "https://node-c.example.com", "persist": True},
            )
            self.assertEqual(res4.status_code, 200)
            self.assertFalse(res4.get_json()["keys"]["COMFYUI_API_KEY"])

            # Invalid concurrency fails fast with 400
            bad = client.post(
                "/api/config",
                headers=headers,
                json={"COMFYUI_CONCURRENCY": "0", "persist": False},
            )
            self.assertEqual(bad.status_code, 400)

    def test_start_colab_supports_ephemeral_and_override_ngrok_domain(self):
        with mock.patch.dict(os.environ, {"H3_NGROK_DOMAIN": "ephemeral"}, clear=False):
            self.assertEqual(start_colab._resolve_ngrok_domain(), "")
        with mock.patch.dict(os.environ, {"H3_NGROK_DOMAIN": "node2.ngrok-free.dev/"}, clear=False):
            self.assertEqual(start_colab._resolve_ngrok_domain(), "https://node2.ngrok-free.dev")

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"tunnels": [{"public_url": "https://rand-1234.ngrok-free.app/"}]}).encode("utf-8")

        with mock.patch.object(start_colab.urllib.request, "urlopen", return_value=_FakeResp()):
            self.assertEqual(start_colab._discover_ngrok_public_url(), "https://rand-1234.ngrok-free.app")


if __name__ == "__main__":
    unittest.main()
