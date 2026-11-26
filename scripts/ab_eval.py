#!/usr/bin/env python3
"""Run isolated image and video prompt comparisons. Dry run never contacts providers."""
import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ab_state import (
    ExperimentError, Journal, file_sha256, fingerprint, money, redactor, run_lock, text_sha256, write_json,
)

MANIFEST = ROOT / "evals" / "golden_shots" / "manifest.json"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
PRICE_KEYS = frozenset(("image", "composition.llm", "composition.claude", "rewrite.llm", "rewrite.claude", "video_per_second"))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def select_cases(catalog, selected_arms, selected_ids, pilot, seed):
    from scripts import ab_variants as variants

    if catalog["schema_version"] != 1 or not _IDENTIFIER.fullmatch(catalog["run_id"]):
        raise ExperimentError("invalid_manifest: expected schema 1 and a valid run_id")
    fixtures = catalog["fixtures"]
    ids = [item["id"] for item in fixtures]
    if len(set(ids)) != len(ids) or any(not _IDENTIFIER.fullmatch(name) for name in ids):
        raise ExperimentError("invalid_manifest: fixture IDs must be unique and path-safe")
    missing = set(selected_ids) - set(ids)
    if missing:
        raise ExperimentError(f"unknown_fixture: {', '.join(sorted(missing))}")
    requested = set(selected_ids)
    cases = []
    for arm in dict.fromkeys(selected_arms or variants.ARMS):
        matches = [item for item in fixtures if arm in item["arms"] and (not requested or item["id"] in requested)]
        if pilot and len(matches) > 3:
            rng = random.Random(f"{seed}/{arm}/pilot")
            normal = [item for item in matches if item["control"]]
            focused = [item for item in matches if not item["control"]]
            rng.shuffle(normal)
            rng.shuffle(focused)
            controls = min(2 if arm == "3D" else 1, len(normal))
            matches = (normal[:controls] + focused + normal[controls:])[:3]
        providers = ("llm", "claude") if arm in variants.COMPOSITION_ARMS | variants.VIDEO_ARMS else ("image",)
        cases.extend((arm, item, provider) for item in matches for provider in providers)
    if not cases:
        raise ExperimentError("empty_selection: no fixtures matched the selected arms")
    unused = requested - {fixture["id"] for _, fixture, _ in cases}
    if unused:
        raise ExperimentError(f"empty_selection: fixtures outside the selected arms: {', '.join(sorted(unused))}")
    return cases


def get_assets(cases, overrides, asset_root):
    resolved, missing = {}, set()
    root = asset_root.resolve()
    for arm, fixture, _ in cases:
        script = fixture["film"]
        keys = [person.get("picture") for person in script.get("cast", ())]
        if script.get("board"):
            keys.append(script["board"])
        if arm in {"3D", "4"}:
            frozen = fixture["frozen_frames"]
            if len(frozen) != len(script["shots"]):
                raise ExperimentError(f"invalid_fixture: {fixture['id']} needs one frozen frame per shot")
            keys.extend(frozen)
        for reference in keys:
            if not isinstance(reference, str) or not reference.startswith("asset:"):
                raise ExperimentError(f"invalid_fixture: {fixture['id']} must use asset: references")
            identifier = reference.removeprefix("asset:")
            if not _IDENTIFIER.fullmatch(identifier):
                raise ExperimentError(f"invalid_asset: {fixture['id']}")
            key = (fixture["id"], identifier)
            if key in resolved or key in missing:
                continue
            asset = overrides.get(key[0], {}).get(identifier, fixture["assets"].get(identifier, {}))
            digest = asset.get("sha256")
            relative = asset.get("path")
            if not relative or not digest:
                missing.add(key)
                continue
            source = (root / relative).resolve()
            if (Path(relative).is_absolute() or not source.is_relative_to(root) or not _SHA256.fullmatch(digest)
                    or source.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")):
                raise ExperimentError(f"invalid_asset: {'/'.join(key)} requires a relative image path and a SHA-256")
            if not source.is_file() or file_sha256(source) != digest:
                raise ExperimentError(f"asset_changed: {'/'.join(key)} is missing or does not match its SHA-256")
            resolved[key] = (source, digest)
    return resolved, missing


def freeze_assets(directory, assets):
    frozen = {}
    by_source = {}
    for (fixture, key), (source, digest) in assets.items():
        target = by_source.setdefault((fixture, source), directory / "inputs" / fixture / (key + source.suffix.lower()))
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                    temporary = Path(output.name)
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, output)
                if file_sha256(temporary) != digest:
                    raise ExperimentError(f"asset_changed: {fixture}/{key} changed during the copy")
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        elif file_sha256(target) != digest:
            raise ExperimentError(f"asset_changed: frozen input {fixture}/{key} differs from the manifest")
        frozen[fixture, key] = target
    return frozen


def local_configuration(use_video):
    from h3ref import comfy, config, rh

    llm = config.llm_settings()
    settings = {
        "image": {"model": config.IMAGE_MODEL, "upstream_version": "unpinned", "resolution": config.IMAGE_RESOLUTION, "quality": config.IMAGE_QUALITY,
                  "endpoint_sha256": fingerprint(config.APIMART_BASE), "usd_cny": config.USD_CNY},
        "text": {"provider": llm["provider"], "composition": llm["composition_model"], "rewrite": llm["rewrite_model"],
                 "claude": config.CLAUDE_MODEL, "endpoint_sha256": fingerprint(llm["base_url"]), "rewriters": ["llm", "claude"], "upstream_versions": "unpinned"},
        "video": None,
    }
    if use_video:
        video = config.video_settings()
        provider = video["provider"]
        comfy_mode = provider == "comfyui"
        endpoint = video["comfyui_url"] if comfy_mode else config.RH_BASE
        settings["video"] = {
            "provider": provider,
            "upstream_version": "unpinned",
            "workflow_id": None if comfy_mode else config.RH_WORKFLOW_ID,
            "workflow_sha256": file_sha256(config.COMFY_GRAPH if comfy_mode else config.RH_GRAPH),
            "endpoint_sha256": fingerprint(endpoint),
            "steps": config.COMFY_PRESETS[video["comfyui_preset"]]["steps"] if comfy_mode else config.H3_STEPS,
            "preset": video["comfyui_preset"] if comfy_mode else None,
            "resolution": video["comfyui_resolution"] if comfy_mode else "544",
            "upscale": video["comfyui_upscale"] if comfy_mode else False,
            "coin_cny": None if comfy_mode else config.RH_COIN_CNY,
        }
        return settings, comfy if comfy_mode else rh, endpoint
    return settings, None, None


class Run:
    def __init__(self, directory, settings, video_backend, endpoint, quotes, guide, *, dry_run, journal=None):
        from h3ref import rewrite

        self.directory = directory
        self.settings = settings
        self.video_backend = video_backend
        self.endpoint = endpoint
        self.quotes = quotes
        self.guide = guide
        self.examples = rewrite.EXAMPLES.read_text(encoding="utf-8")
        self.dry_run = dry_run
        self.journal = journal
        self.calls = Counter()
        self.unpriced = set()
        self.estimated = Decimal(0)
        self.prompt_lengths = {}
        self.redact = redactor(os.environ)

    def price(self, stage, seconds=None):
        self.calls[stage] += 1
        rate = self.quotes.get(stage)
        if rate is None:
            self.unpriced.add(stage)
            return None
        price = money(rate)
        if seconds is not None:
            price = max(price * Decimal(seconds), money(self.video_backend.estimate_cny(seconds)))
        self.estimated += price
        return price

    def describe(self, key, prompt):
        self.prompt_lengths[key] = len(prompt)

    def text(self, key, stage, request, send, stub):
        estimate = self.price(stage)
        self.describe(key, request)
        if self.dry_run:
            return stub()
        if estimate is None:
            raise ExperimentError(f"unpriced_stage: {stage}")
        path = self.directory / "cache" / f"{key}.json"
        if self.redact(request) != request:
            raise ExperimentError(f"secret_in_request: {key}")
        request_sha = text_sha256(request)
        step = self.journal.reserve(key, estimate)
        if step["status"] == "done":
            self.check_output(path, step, request_sha)
            return read_json(path)
        self.journal.update(key, request_sha256=request_sha)
        result = send()
        # Never put provider credentials into prompts or reproducibility artifacts.
        content = json.dumps(result, ensure_ascii=False)
        if self.redact(content) != content:
            raise ExperimentError(f"secret_in_response: {key}")
        write_json(path, result)
        self.journal.update(key, status="done", output_sha256=file_sha256(path))
        return result

    def check_output(self, path, step, request_sha):
        if step.get("request_sha256") != request_sha:
            raise ExperimentError("fingerprint_changed: a paid request differs from the original")
        if not path.is_file() or file_sha256(path) != step.get("output_sha256"):
            raise ExperimentError("artifact_changed: restore the original output before resuming")

    def image(self, key, prompt, references, aspect):
        from h3ref import config, frames

        if len(references) > config.MAX_IMAGES:
            raise ExperimentError(f"too_many_references: {key} exceeds the image reference limit")
        estimate = self.price("image")
        self.describe(key, prompt)
        if self.dry_run:
            return None
        if estimate is None:
            raise ExperimentError("unpriced_stage: image")
        path = self.directory / "cache" / f"{key}.png"
        if self.redact(prompt) != prompt:
            raise ExperimentError(f"secret_in_request: {key}")
        prompt_sha = text_sha256(prompt)
        request_sha = fingerprint({"prompt_sha256": prompt_sha, "references": [file_sha256(p) for p in references],
                                   "aspect": aspect})
        step = self.journal.reserve(key, estimate)
        status = step["status"]
        if status == "done":
            self.check_output(path, step, request_sha)
            return path
        if status == "failed":
            return None
        if status == "submitted" and step.get("request_sha256") != request_sha:
            raise ExperimentError("fingerprint_changed: restore the original image request before polling")
        self.journal.update(key, request_sha256=request_sha, prompt_sha256=prompt_sha)
        path.parent.mkdir(parents=True, exist_ok=True)

        def record(**fields):
            if fields.get("task_failed"):
                self.journal.update(key, status="failed", moderation_failed=bool(fields.get("moderation_error")))
            elif fields.get("task"):
                self.journal.update(key, status="submitted", task=fields["task"])

        try:
            frames.draw(prompt, references, aspect, path, record=record, task=step.get("task"),
                        settle=lambda charge: self.journal.update(key, actual_cny=None if charge is None else str(money(charge))))
        except frames.ContentPolicyError:
            self.journal.update(key, status="failed", moderation_failed=True)
            return None
        except frames.SubmissionRejected:
            self.journal.update(key, status="failed")
            raise
        except RuntimeError:
            if self.journal.steps[key]["status"] == "failed":
                return None
            raise
        self.journal.update(key, status="done", output_sha256=file_sha256(path))
        return path

    def video(self, key, prompt, references, seconds, aspect, seed):
        from h3ref import http

        estimate = self.price("video_per_second", seconds)
        self.describe(key, prompt)
        if self.dry_run:
            return None
        if estimate is None:
            raise ExperimentError("unpriced_stage: video")
        path = self.directory / "cache" / f"{key}.mp4"
        if self.redact(prompt) != prompt:
            raise ExperimentError(f"secret_in_request: {key}")
        prompt_sha = text_sha256(prompt)
        request_sha = fingerprint({"prompt_sha256": prompt_sha, "references": [file_sha256(p) for p in references],
                                   "aspect": aspect, "seconds": seconds, "seed": seed})
        step = self.journal.reserve(key, estimate)
        status = step["status"]
        if status == "done":
            self.check_output(path, step, request_sha)
            return path
        if status == "failed":
            return None
        backend = self.video_backend
        is_comfy = self.settings["video"]["provider"] == "comfyui"
        endpoint = {"base_url": self.endpoint} if is_comfy else {}
        if status == "submitted" and step.get("request_sha256") != request_sha:
            raise ExperimentError("fingerprint_changed: restore the original video request before polling")
        self.journal.update(key, request_sha256=request_sha, prompt_sha256=prompt_sha)
        task = step.get("task")
        if not task:
            names = backend.upload_many(references, **endpoint) if is_comfy else [backend.upload(path) for path in references]
            graph = backend.graph(prompt, names, seconds, aspect, seed)
            try:
                task = backend.submit(graph, **endpoint)
            except backend.SubmissionRejected:
                self.journal.update(key, status="failed")
                raise
            self.journal.update(key, status="submitted", task=task)
        for _ in range(1440 if is_comfy else 360):
            result = backend.poll(task, **endpoint) if is_comfy else backend.poll(task, report_billing=True)
            if result["state"] == "failed":
                self.journal.update(key, status="failed")
                return None
            if result["state"] == "done":
                charge = result.get("cny") if result.get("billing_known") else None
                self.journal.update(key, actual_cny=None if charge is None else str(money(charge)))
                path.parent.mkdir(parents=True, exist_ok=True)
                if is_comfy:
                    backend.download(result["url"], path)
                else:
                    http.download(result["url"], path)
                self.journal.update(key, status="done", output_sha256=file_sha256(path))
                return path
            time.sleep(5 if is_comfy else 20)
        raise ExperimentError(f"poll_timed_out: resume the submitted video step {key}")

    def result(self, label, stem, paths, *, expected):
        records = [step for key, step in self.journal.steps.items() if key.startswith(stem + "/")] if self.journal else []
        exact = bool(records) and all(step["actual_cny"] is not None for step in records)
        return {"label": label, "media": paths, "complete": len(paths) == expected,
                "cny": sum(float(step["actual_cny"]) for step in records) if exact else None,
                "moderation_failed": any(step.get("moderation_failed") for step in records), "retries": 0}


def stage_composition(run, key, provider, script, shot, history, rules):
    from h3ref import config, frames, http, rewrite
    from scripts import ab_variants as variants

    def infer(messages):
        canonical = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))

        def send():
            if provider == "claude":
                prompt = "\n\n".join(message["content"] for message in messages)
                return {"choices": [{"message": {"content": rewrite._claude(prompt)}}]}
            llm = config.llm_settings()
            response = http.request(llm["base_url"] + "/chat/completions", config.llm_key(),
                                    {"model": llm["composition_model"], "messages": messages,
                                     "max_tokens": 8000, "stream": False},
                                    timeout=600, tries=1)
            return response

        sent = "\n\n".join(message["content"] for message in messages) if provider == "claude" else canonical
        return run.text(key, f"composition.{provider}", sent, send, lambda: variants.stub_composition(messages))

    previous, locked = history
    # Composition uses written appearance descriptions, with no need for local image paths.
    source = {**script, "cast": [{k: v for k, v in person.items() if k != "picture"}
                                  for person in script.get("cast", ())]}
    return frames.describe_composition(source, shot, previous_composition=previous, locked_looks=locked,
                                       rules_override=rules, infer=infer, max_retries=0)


def run_side(run, case, variant, resolved, seed):
    from h3ref import film, frames, rewrite
    from scripts import ab_variants as variants

    arm, fixture, provider = case
    identity = fixture["id"]
    stem = f"{arm}/{identity}/{provider}/{variant}"
    options = variants.prompt_options(arm, variant, run.examples)
    script = deepcopy(fixture["film"])

    def ref_path(reference):
        name = reference.removeprefix("asset:")
        return resolved.get((identity, name), run.directory / "inputs" / identity / f"{name}.png")

    for person in script.get("cast", []):
        person["picture"] = str(ref_path(person["picture"]))
    if script.get("board"):
        script["board"] = str(ref_path(script["board"]))
    script["shots"] = film.normalize_shots(script["shots"], strict=True)
    segments = film.plan(script["shots"])
    if len(segments) != 1 or segments[0]["end"] - segments[0]["start"] > 15:
        raise ExperimentError(f"invalid_fixture: {identity} must fit in one H3 segment")

    composition = {}
    previous, locked = None, {}
    image_paths = []
    expected = (len(script["shots"]) if arm in variants.IMAGE_ARMS else 0) + (arm in variants.VIDEO_ARMS)
    if arm in variants.IMAGE_ARMS:
        for number, shot in enumerate(script["shots"], 1):
            if arm in variants.COMPOSITION_ARMS:
                previous = stage_composition(run, f"{stem}/compose-{number}", provider, script, shot,
                                             (previous, locked), options.get("composition_rules"))
                if previous.get("version") != 3:
                    raise ExperimentError(f"composition_version: {stem} needs a composition v3 response")
                for character in previous["characters"]:
                    locked.setdefault(character["name"], character["locked_look"])
                composition[f"s1-{number}"] = previous
            cast = [person for person in script.get("cast", ()) if film.in_shots(person, [shot])]
            background = script.get("board")
            references = [Path(path) for path in film.frame_reference_paths(script, shot, {})]
            prompt = frames.prompt(script, shot, cast, bool(background), previous,
                                   neutral_words=options.get("neutral_words"),
                                   tail_override=options.get("tail_override"),
                                   shot_state_note=options.get("image_state_note"))
            frame = run.image(f"{stem}/image-{number}", prompt, references, script["aspect"])
            image_paths.append(frame)
    if arm in variants.VIDEO_ARMS:
        segment = segments[0]
        frozen = image_paths if arm == "2A" else [ref_path(ref) for ref in fixture["frozen_frames"]]
        if run.dry_run and arm == "2A":
            frozen = [run.directory / "cache" / f"{stem}/image-{n}.png"
                      for n in range(1, len(script["shots"]) + 1)]
        if any(path is None for path in frozen):
            return run.result(f"{provider}/{variant}", stem, [path for path in image_paths if path], expected=expected)
        pictures = film.refs(script, segment, "shot")
        if {item["shot"] for item in pictures if item["kind"] == "frame"} != set(range(len(frozen))):
            raise ExperimentError(f"too_many_references: {identity} must keep every shot's opening frame")
        inputs = [frozen[item["shot"]] if item["kind"] == "frame" else Path(item["path"]) for item in pictures]
        source = film.compile_prompt(script, segment, pictures, 1, compositions=composition,
                                     shot_state_note=options.get("video_state_note"))
        n_images = len(inputs)
        aspect, seconds = script["aspect"], segment["seconds"]

        def infer(request):
            send = (lambda: rewrite._claude(request)) if provider == "claude" else (lambda: rewrite._llm(request, tries=1))
            return run.text(f"{stem}/rewrite", f"rewrite.{provider}", request, send,
                            lambda: variants.stub_rewrite(n_images, aspect))

        prompt = rewrite.rewrite(source, n_images, seconds, aspect, max_retries=0, infer=infer, rewriter_override=provider,
                                 rules_override=options.get("rules_override"), guide_override=run.guide,
                                 examples_override=options.get("examples_override"))
        matched_seed = int(fingerprint([seed, arm, identity, provider])[:8], 16)
        video = run.video(f"{stem}/video", prompt, inputs, seconds, aspect, matched_seed)
        if video:
            image_paths.append(video)
    return run.result(f"{provider}/{variant}", stem, [path for path in image_paths if path], expected=expected)


def run_pairs(run, cases, resolved, seed):
    from scripts import ab_variants as variants

    pairs = []
    for case in cases:
        arm, fixture, provider = case
        base = run_side(run, case, "baseline", resolved, seed)
        for variant in variants.variant_names(arm):
            compared = run_side(run, case, variant, resolved, seed)
            pairs.append({"id": f"{arm}/{fixture['id']}/{provider}/{variant}", "arm": arm,
                          "description": fixture["description"], "sides": [base, compared]})
    return pairs


def live_account_fingerprints(plan, configuration, endpoint):
    from h3ref import config, rewrite

    calls = plan.calls
    accounts = {}
    claude = rewrite.claude_bin() if any(stage.endswith(".claude") for stage in calls) else None
    if any(stage.endswith(".claude") for stage in calls):
        if not claude or not (Path(claude).is_file() or shutil.which(claude)):
            raise ExperimentError("provider_missing: install and authenticate the Claude CLI")
        # A signed-in seat may not expose a stable account ID. Never store a token.
        accounts["claude"] = fingerprint(config.env("CLAUDE_CODE_OAUTH_TOKEN")) if config.env("CLAUDE_CODE_OAUTH_TOKEN") else "local-seat"
    if any(stage.endswith(".llm") for stage in calls):
        accounts["llm"] = fingerprint(config.llm_key())
    if "image" in calls:
        accounts["image"] = fingerprint(config.apimart_key())
    if "video_per_second" in calls:
        if configuration["video"]["provider"] == "runninghub":
            accounts["video"] = fingerprint(config.rh_key())
        elif not endpoint or (config.comfy_requires_api_key([endpoint]) and not config.comfy_key()):
            raise ExperimentError("provider_missing: configure a single ComfyUI endpoint and its key")
        else:
            accounts["video"] = fingerprint(config.comfy_key())
    return accounts


def prompt_growth(lengths):
    comparisons = {}
    for key, length in lengths.items():
        arm, fixture, provider, variant, stage = key.split("/")
        if variant == "baseline" or not (stage.startswith("image-") or stage == "video"):
            continue
        baseline = lengths.get(f"{arm}/{fixture}/{provider}/baseline/{stage}")
        if baseline:
            comparisons[key] = {"baseline": baseline, "variant": length,
                                "within_15_percent": 100 * length <= 115 * baseline}
    return comparisons

def parser():
    from scripts import ab_variants as variants

    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--manifest", type=Path, default=MANIFEST)
    cli.add_argument("--arm", action="append", choices=variants.ARMS)
    cli.add_argument("--fixture", action="append", default=[])
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--seed", type=int, default=20261124)
    cli.add_argument("--pilot", action="store_true", help="Select up to three fixtures per arm deterministically")
    cli.add_argument("--asset-root", type=Path, default=ROOT)
    cli.add_argument("--assets", type=Path, help="JSON mapping fixture IDs and asset keys to relative paths and SHA-256 values")
    cli.add_argument("--guide", type=Path, help="A frozen local copy of the official H3 prompt guide")
    cli.add_argument("--quotes", type=Path, help="Verified worst-case CNY prices for each call type")
    cli.add_argument("--max-cny", type=money)
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument("--resume", action="store_true")
    return cli


def execute(args):
    from scripts import ab_variants as variants
    from scripts.ab_review import write_review

    if args.dry_run and args.resume:
        raise ExperimentError("invalid_arguments: --dry-run and --resume are independent modes")
    if not args.dry_run and not args.max_cny:
        raise ExperimentError("budget_required: paid runs require explicit --max-cny and --quotes")
    quotes = read_json(args.quotes) if args.quotes else {}
    if (not isinstance(quotes, dict) or set(quotes) - PRICE_KEYS
            or any(money(price) <= 0 for price in quotes.values())):
        raise ExperimentError("invalid_prices: quote each call type at a positive CNY amount")
    catalog = read_json(args.manifest)
    cases = select_cases(catalog, args.arm, args.fixture, args.pilot, args.seed)
    resolved, missing = get_assets(cases, read_json(args.assets) if args.assets else {}, args.asset_root)
    use_video = any(arm in variants.VIDEO_ARMS for arm, _, _ in cases)
    config_snapshot, video_backend, endpoint = local_configuration(use_video)
    guide = args.guide.read_text(encoding="utf-8") if args.guide else ""
    guide_needed = use_video and not guide.strip()
    guide_text = guide if guide.strip() else "Offline guide placeholder. Supply a verified frozen official guide for live video comparisons."
    directory = args.out.resolve()
    dry = Run(directory, config_snapshot, video_backend, endpoint, quotes, guide_text, dry_run=True)
    preview = run_pairs(dry, cases, {key: source for key, (source, _) in resolved.items()}, args.seed)
    if not args.dry_run:
        if missing or guide_needed or dry.unpriced:
            raise ExperimentError("paid_inputs_missing: use --dry-run and complete missing_assets, guide_needed and unpriced entries")
        if dry.estimated > args.max_cny:
            raise ExperimentError(f"budget_exhausted: the selected calls need ¥{dry.estimated}, above the ¥{args.max_cny} cap")
        config_snapshot["account_fingerprints"] = live_account_fingerprints(dry, config_snapshot, endpoint)
    provenance = {
        "catalog_sha256": file_sha256(args.manifest),
        "settings": config_snapshot,
        "seed": args.seed,
        "cases": [[arm, fixture["id"], provider] for arm, fixture, provider in cases],
        "assets": {"/".join(key): {"path": source.relative_to(args.asset_root.resolve()).as_posix(), "sha256": digest}
                   for key, (source, digest) in resolved.items()},
        "guide_sha256": text_sha256(guide) if not guide_needed and args.guide else None,
        "quotes": quotes,
        "rules_sha256": {name: file_sha256(ROOT / name) for name in (
            "h3ref/frames.py", "h3ref/film.py", "h3ref/staging.py", "h3ref/rewrite.py", "prompts/h3_examples.md",
            "scripts/ab_variants.py", "scripts/ab_eval.py",
        )},
    }
    run_hash = fingerprint(provenance)
    summary = {
        "fingerprint": run_hash, "pairs": len(preview), "calls": dict(dry.calls), "unpriced": sorted(dry.unpriced),
        "quoted_cny": None if dry.unpriced else str(dry.estimated),
        "within_max_cny": None if dry.unpriced or args.max_cny is None else dry.estimated <= args.max_cny,
        "missing_assets": ["/".join(key) for key in sorted(missing)],
        "guide_needed": guide_needed, "prompt_chars": dry.prompt_lengths, "reproducibility": "unreproducible",
        "prompt_growth": prompt_growth(dry.prompt_lengths), "provenance": provenance,
    }
    if args.dry_run:
        with run_lock(directory):
            if (directory / "ab_state.json").exists():
                raise ExperimentError("state_exists: choose a separate --out directory for a dry run")
            run_id = f"{catalog['run_id']}-dry-{run_hash[:12]}"
            write_json(directory / "dry_run.json", {**summary, "run_id": run_id, "charged_cny": "0"})
            write_review(directory, preview, run_id=run_id, seed=args.seed, dry_run=True, redact=dry.redact)
        return {key: value for key, value in summary.items() if key not in ("prompt_chars", "prompt_growth", "provenance")}
    with run_lock(directory):
        state_path = directory / "ab_state.json"
        run_id = read_json(state_path)["run_id"] if args.resume and state_path.is_file() else f"{catalog['run_id']}-{uuid.uuid4().hex[:12]}"
        journal = Journal(state_path, run_id, run_hash, args.max_cny, resume=args.resume)
        write_json(directory / "run_manifest.json", {**summary, "run_id": run_id})
        frozen = freeze_assets(directory, resolved)
        run = Run(directory, config_snapshot, video_backend, endpoint, quotes, guide, dry_run=False, journal=journal)
        pairs = run_pairs(run, cases, frozen, args.seed)
        committed = journal.committed_cny()
        stats = {"run_id": run_id, "pairs": len(pairs), "reserved_or_charged_cny": str(committed),
                 "over_budget": committed > journal.cap}
        write_json(directory / "run_manifest.json", {**summary, **stats, "steps": journal.steps,
                                                     "actual_prompt_chars": run.prompt_lengths,
                                                     "actual_prompt_growth": prompt_growth(run.prompt_lengths)})
        write_review(directory, pairs, run_id=run_id, seed=args.seed, dry_run=False, redact=run.redact)
        return stats


def main():
    if "--dry-run" in sys.argv[1:]:
        os.environ["H3_SKIP_DOTENV"] = "1"
    try:
        result = execute(parser().parse_args())
        print(json.dumps(result, ensure_ascii=True))
        if result.get("over_budget"):
            print("ab_eval: budget_exceeded: see the recorded actual charges in run_manifest.json", file=sys.stderr)
            return 1
    except (ValueError, OSError, RuntimeError, TypeError, KeyError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and isinstance(exc.code, int):
            return exc.code
        clean = redactor(os.environ)(exc).splitlines()
        print(f"ab_eval: {' '.join(clean)[:500]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
