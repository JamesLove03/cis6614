import argparse
import json
from importlib.metadata import version

from .data import prepare, write_json
from .detector import Detector, MODEL, PRICE, QUESTION, RESERVATION, ROOT, input_state, question_specs
from .report import report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "diagnostic", "pilot", "validation", "test", "status"))
    parser.add_argument("--execute", action="store_true", help="Allow paid Jev calls")
    parser.add_argument("--variant", choices=("A", "B", "C"), default="A")
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = prepare()
        print(json.dumps({"revision": manifest["revision"], "splits": manifest["splits"]}, indent=2))
        return
    detector = Detector(variant=args.variant)
    result_root = ROOT / "results" if args.variant == "A" else ROOT / "results" / args.variant
    if args.command == "status":
        print(json.dumps(detector.totals(), indent=2))
        return
    manifest = json.loads((ROOT / "data/manifest.json").read_text())
    if manifest["model"] != MODEL or manifest["question"] != QUESTION:
        raise SystemExit("Detector differs from the frozen dataset manifest")
    if args.command == "diagnostic":
        if args.variant != "C":
            raise SystemExit("Diagnostic is only defined for C")
        source = {r["id"]: r for r in json.loads((ROOT / "data/test.json").read_text())}
        cases = json.loads((ROOT / "prompts/C-cases.json").read_text())
        rows = [{**source[case["id"]], "group": case["group"]} for case in cases]
        for row in rows:
            row.update(split="diagnostic", length_bin=sum(len(row["prompt"]) > b for b in (256, 1024, 4096)))
    else:
        rows = json.loads((ROOT / f"data/{args.command}.json").read_text())
    specs = question_specs(args.variant)
    missing = [r for r in rows if not detector.cached(r["prompt"])]
    print(json.dumps({"variant": args.variant, "stage": args.command, "selected": len(rows), "uncached": len(missing),
                      "state_template": input_state("<complete original dataset prompt>", args.variant),
                      "questions": specs,
                      "estimated_usd_at_4_chars_per_token": sum(
                          (len(json.dumps(input_state(r["prompt"], args.variant))) / 4 + len(json.dumps(specs)) / 4 + 256) * PRICE for r in missing),
                      "maximum_reserved_usd": len(missing) * RESERVATION,
                      "ledger": detector.totals(), "execute": args.execute}, indent=2), flush=True)
    if not args.execute:
        return
    if args.command in ("validation", "test"):
        previous = "pilot" if args.command == "validation" else "validation"
        if args.variant == "C" and args.command == "validation":
            previous = "diagnostic"
        summary_path = result_root / previous / "summary.json"
        if not summary_path.exists():
            raise SystemExit(f"Run {previous} first")
        summary = json.loads(summary_path.read_text())
        if summary["evaluated"] + summary.get("status_counts", {}).get("oversized", 0) != summary["selected"]:
            raise SystemExit(f"Resolve incomplete {previous} before proceeding")
        previous_environment = json.loads((summary_path.parent / "environment.json").read_text())
        if "questions" in previous_environment and previous_environment["questions"] != specs:
            raise SystemExit("Questions changed since the previous stage")
    directory = result_root / args.command
    directory.mkdir(parents=True, exist_ok=True)
    environment = {
        "model": MODEL, "question": QUESTION if args.variant != "C" else None,
        "questions": specs, "decision_rule": "any >= 0.5" if args.variant == "C" else "probability >= 0.5",
        "dataset_revision": manifest["revision"],
        "stage_start_cost": detector.totals()["accounted_usd"],
        "variant": args.variant, "state_template": input_state("<complete original dataset prompt>", args.variant),
        "packages": {p: version(p) for p in ("langchain-typesafe", "langchain-core", "httpx2", "numpy")}}
    environment_path = directory / "environment.json"
    if environment_path.exists():
        old = json.loads(environment_path.read_text())
        if any(old.get(k) != environment[k] for k in ("model", "question", "dataset_revision")):
            raise SystemExit("Existing experiment configuration differs")
        if "state_template" in old and old["state_template"] != environment["state_template"]:
            raise SystemExit("Existing experiment state format differs")
        if "questions" in old and old["questions"] != specs:
            raise SystemExit("Existing experiment questions differ")
        environment["stage_start_cost"] = old.get("stage_start_cost", environment["stage_start_cost"])
    write_json(environment_path, environment)
    output = []
    stopped = False
    for i, row in enumerate(rows):
        ceiling = environment["stage_start_cost"] + .01 if args.command == "diagnostic" else 1.0
        result = {"status": "not_run", "injection_probability": None} if stopped else detector.detect(row["prompt"], execute=True, max_project_cost=ceiling)
        output.append({**row, **result})
        with (directory / "progress.jsonl").open("a") as f:
            f.write(json.dumps({**row, **result}, ensure_ascii=False) + "\n")
        if result["status"] not in ("ok", "oversized", "not_run"):
            stopped = True
        if (i + 1) % 20 == 0:
            print(f"{i + 1}/{len(rows)}; accounted ${detector.totals()['accounted_usd']:.6f}", flush=True)
    summary = report(output, directory, detector.totals())
    print(json.dumps(summary, indent=2))
    if stopped:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
