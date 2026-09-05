from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dotenv_ai_overrides_load_before_ai_service(tmp_path):
    (tmp_path / ".env").write_text(
        "OPENAI_RESPONSE_MODEL=dotenv-response-model\n"
        "OPENAI_EMBEDDING_MODEL=dotenv-embedding-model\n"
        "OPENAI_TIMEOUT_SECONDS=73\n"
        "OPENAI_MAX_RETRIES=3\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    for name in (
        "OPENAI_RESPONSE_MODEL",
        "OPENAI_EMBEDDING_MODEL",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_RETRIES",
    ):
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from backend import ai_service; "
                "print(ai_service.RESPONSE_MODEL); "
                "print(ai_service.EMBEDDING_MODEL); "
                "print(ai_service.OPENAI_TIMEOUT_SECONDS); "
                "print(ai_service.OPENAI_MAX_RETRIES)"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "dotenv-response-model",
        "dotenv-embedding-model",
        "73.0",
        "3",
    ]


def test_exported_environment_takes_precedence_over_dotenv(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_RESPONSE_MODEL=dotenv-model\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["OPENAI_RESPONSE_MODEL"] = "exported-model"

    result = subprocess.run(
        [sys.executable, "-c", "from backend.ai_service import RESPONSE_MODEL; print(RESPONSE_MODEL)"],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "exported-model"
