"""Publish the app to a Hugging Face Space (Docker SDK).

Run after `python -m app.ingest && python -m app.index` and `hf auth login`:

    python deploy/huggingface/deploy.py --space <hf-username>/icici-mf-facts-assistant

It assembles a clean folder (code, registry, pre-built chunks and index; no raw PDFs, logs, or
.env), uploads it, and stores GEMINI_API_KEY / SARVAM_API_KEY from .env as private Space secrets.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
# Pre-built runtime data: only what retrieval and answering need.
DATA_FILES = (
    "data/source_registry.json",
    "data/processed/chunks.jsonl",
    "data/indexes/chunks.faiss",
    "data/indexes/index_manifest.json",
)
EMPTY_DIRS = ("data/raw_sources", "data/extracted_text", "data/logs")
SECRETS = ("GEMINI_API_KEY", "SARVAM_API_KEY")


def stage(target: Path) -> None:
    shutil.copytree(ROOT / "app", target / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "pyproject.toml", target / "pyproject.toml")
    shutil.copy2(HERE / "Dockerfile", target / "Dockerfile")
    shutil.copy2(HERE / "SPACE_README.md", target / "README.md")
    for relative in DATA_FILES:
        source = ROOT / relative
        if not source.is_file():
            sys.exit(f"Missing {relative}: run `python -m app.ingest && python -m app.index` first.")
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / relative)
    for relative in EMPTY_DIRS:
        (target / relative).mkdir(parents=True, exist_ok=True)
        (target / relative / ".gitkeep").touch()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--space", required=True, help="Space id, e.g. username/icici-mf-facts-assistant")
    args = parser.parse_args()

    api = HfApi()
    api.create_repo(args.space, repo_type="space", space_sdk="docker", exist_ok=True)
    env = dotenv_values(ROOT / ".env")
    for name in SECRETS:
        if env.get(name):
            api.add_space_secret(args.space, name, env[name])  # stored privately on Hugging Face
            print(f"secret set: {name}")
    with tempfile.TemporaryDirectory() as folder:
        stage(Path(folder))
        api.upload_folder(repo_id=args.space, repo_type="space", folder_path=folder, commit_message="Deploy facts-only assistant")
    print(f"Deployed: https://huggingface.co/spaces/{args.space}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
