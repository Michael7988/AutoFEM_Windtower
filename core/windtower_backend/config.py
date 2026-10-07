from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency installed by requirements
    load_dotenv = None


BACKEND_ROOT = Path(__file__).resolve().parent
CORE_ROOT = BACKEND_ROOT.parent
PROJECT_ROOT = CORE_ROOT.parent
EXAMPLE_ROOT = PROJECT_ROOT / "tests" / "fixtures"

# Back-compatible name used throughout the existing backend and tests.
ROOT = PROJECT_ROOT
DESKTOP_MODE = os.getenv("WINDTOWER_DESKTOP_MODE", "").strip().lower() in {"1", "true", "yes"}
DATA_ROOT = Path(os.getenv("WINDTOWER_DATA_ROOT", "")).expanduser() if os.getenv("WINDTOWER_DATA_ROOT") else None
WORK_ROOT = Path(os.getenv("WINDTOWER_WORK_ROOT", "")).expanduser() if os.getenv("WINDTOWER_WORK_ROOT") else None
LOG_ROOT = Path(os.getenv("WINDTOWER_LOG_ROOT", "")).expanduser() if os.getenv("WINDTOWER_LOG_ROOT") else None
CACHE_ROOT = Path(os.getenv("WINDTOWER_CACHE_ROOT", "")).expanduser() if os.getenv("WINDTOWER_CACHE_ROOT") else None
CONFIG_ROOT = Path(os.getenv("WINDTOWER_CONFIG_ROOT", "")).expanduser() if os.getenv("WINDTOWER_CONFIG_ROOT") else None

STORAGE_ROOT = (DATA_ROOT / "projects") if DATA_ROOT else (BACKEND_ROOT / "storage")
INPUT_BATCH_ROOT = (DATA_ROOT / "input-batches") if DATA_ROOT else (STORAGE_ROOT / "input_batches")
METADATA_DB = (DATA_ROOT / "metadata.sqlite3") if DATA_ROOT else (STORAGE_ROOT / "metadata.sqlite3")
ABAQUSWORK = WORK_ROOT if WORK_ROOT else (PROJECT_ROOT / "ABAQUSwork")
LOG_ROOT = LOG_ROOT or (BACKEND_ROOT / "logs")
CACHE_ROOT = CACHE_ROOT or (BACKEND_ROOT / ".cache")
CONFIG_ROOT = CONFIG_ROOT or BACKEND_ROOT
BACKUP_ROOT = (DATA_ROOT.parent / "backups") if DATA_ROOT else (BACKEND_ROOT / "backups")
KNOWLEDGE_ROOT = (DATA_ROOT / "knowledge") if DATA_ROOT else (STORAGE_ROOT / "knowledge")
MODEL_LIBRARY_ROOT = (DATA_ROOT / "model-library") if DATA_ROOT else (STORAGE_ROOT / "model_library")
LLM_API_CONFIG = CONFIG_ROOT / "llm_api_config.yaml"
DESKTOP_SETTINGS_FILE = CONFIG_ROOT / "desktop-settings.json"


@dataclass
class Settings:
    qwen_api_key: str | None
    qwen_model: str
    qwen_engineering_model: str
    qwen_fem_model: str
    qwen_base_url: str
    qwen_temperature: float
    qwen_max_tokens: int
    qwen_timeout: int
    abaqus_cmd: str
    cors_origins: list[str]
    api_token: str | None = None
    upload_file_limit_bytes: int = 50 * 1024 * 1024
    upload_batch_limit_bytes: int = 200 * 1024 * 1024
    upload_unpacked_limit_bytes: int = 500 * 1024 * 1024
    upload_max_compression_ratio: float = 100.0
    abaqus_max_concurrent_jobs: int = 2
    abaqus_job_cpus: int = 1
    abaqus_job_timeout_seconds: int = 14400
    abaqus_odb_timeout_seconds: int = 1800
    abaqus_min_free_gb: float = 5.0
    desktop_mode: bool = False
    desktop_token: str | None = None
    data_root: Path | None = None
    work_root: Path | None = None
    log_root: Path | None = None
    cache_root: Path | None = None
    config_root: Path | None = None
    metadata_db: Path | None = None
    docling_service_url: str | None = None
    docling_timeout_seconds: int = 300
    evidence_token_budget: int = 12000
    evidence_catalog_token_budget: int = 1500
    evidence_max_fragments: int = 30
    evidence_top_k_per_field: int = 5
    # Named connection profile used for audit/reporting.  ``legacy`` preserves
    # the historical QWEN_API_KEY + QWEN_BASE_URL behaviour.
    qwen_provider_profile: str = "auto"


def load_settings() -> Settings:
    if load_dotenv:
        load_dotenv(BACKEND_ROOT / ".env")
        load_dotenv(ROOT / ".env")
    yaml_config = load_llm_api_config(LLM_API_CONFIG)
    qwen_config = yaml_config.get("qwen", {}) if isinstance(yaml_config.get("qwen"), dict) else {}
    desktop_config = load_desktop_settings()
    default_origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ]
    if DESKTOP_MODE:
        # Electron's packaged file:// renderer sends the literal Origin: null.
        # Requests still require the per-launch desktop token.
        default_origins.append("null")
    extra_origins = [
        item.strip()
        for item in os.getenv("WINDTOWER_CORS_ORIGINS", "").split(",")
        if item.strip()
    ]
    legacy_qwen_model = os.getenv(
        "QWEN_MODEL",
        _as_str(desktop_config.get("qwen_model"), _as_str(qwen_config.get("model"), "qwen3.8-max")),
    )
    qwen_profile, qwen_api_key, qwen_base_url = _resolve_qwen_connection(
        model=legacy_qwen_model,
        desktop_config=desktop_config,
        qwen_config=qwen_config,
    )
    return Settings(
        # Credentials are environment-only; YAML is restricted to non-secret
        # provider options so a source archive cannot leak the API key.
        qwen_api_key=qwen_api_key,
        qwen_model=legacy_qwen_model,
        qwen_engineering_model=os.getenv(
            "QWEN_ENGINEERING_MODEL",
            _as_str(desktop_config.get("qwen_engineering_model"), legacy_qwen_model),
        ),
        qwen_fem_model=os.getenv(
            "QWEN_FEM_MODEL",
            _as_str(desktop_config.get("qwen_fem_model"), legacy_qwen_model),
        ),
        qwen_base_url=qwen_base_url,
        qwen_temperature=float(os.getenv("QWEN_TEMPERATURE", _as_str(qwen_config.get("temperature"), "0.7"))),
        qwen_max_tokens=int(os.getenv("QWEN_MAX_TOKENS", _as_str(qwen_config.get("max_tokens"), "4000"))),
        qwen_timeout=int(os.getenv("QWEN_TIMEOUT", _as_str(qwen_config.get("timeout"), "60"))),
        abaqus_cmd=os.getenv(
            "ABAQUS_COMMAND",
            os.getenv("ABAQUS_CMD", _as_str(desktop_config.get("abaqus_command"), r"C:\SIMULIA\Commands\abaqus.bat")),
        ),
        cors_origins=sorted(set(default_origins + extra_origins)),
        api_token=_as_optional_str(os.getenv("WINDTOWER_API_TOKEN")),
        upload_file_limit_bytes=max(1, int(os.getenv("WINDTOWER_UPLOAD_FILE_LIMIT_MB", "50"))) * 1024 * 1024,
        upload_batch_limit_bytes=max(1, int(os.getenv("WINDTOWER_UPLOAD_BATCH_LIMIT_MB", "200"))) * 1024 * 1024,
        upload_unpacked_limit_bytes=max(1, int(os.getenv("WINDTOWER_UPLOAD_UNPACKED_LIMIT_MB", "500"))) * 1024 * 1024,
        upload_max_compression_ratio=max(1.0, float(os.getenv("WINDTOWER_UPLOAD_MAX_COMPRESSION_RATIO", "100"))),
        abaqus_max_concurrent_jobs=max(1, int(os.getenv("ABAQUS_MAX_CONCURRENT_JOBS", "2"))),
        abaqus_job_cpus=max(1, int(os.getenv("ABAQUS_JOB_CPUS", "1"))),
        abaqus_job_timeout_seconds=max(60, int(os.getenv("ABAQUS_JOB_TIMEOUT_SECONDS", "14400"))),
        abaqus_odb_timeout_seconds=max(60, int(os.getenv("ABAQUS_ODB_TIMEOUT_SECONDS", "1800"))),
        abaqus_min_free_gb=max(0.0, float(os.getenv("ABAQUS_MIN_FREE_GB", "5"))),
        desktop_mode=DESKTOP_MODE,
        desktop_token=_as_optional_str(os.getenv("WINDTOWER_DESKTOP_TOKEN")),
        data_root=DATA_ROOT,
        work_root=WORK_ROOT,
        log_root=LOG_ROOT,
        cache_root=CACHE_ROOT,
        config_root=CONFIG_ROOT,
        metadata_db=METADATA_DB,
        docling_service_url=_as_optional_str(os.getenv("DOCLING_SERVICE_URL")),
        docling_timeout_seconds=max(10, int(os.getenv("DOCLING_TIMEOUT_SECONDS", "300"))),
        evidence_token_budget=max(1000, int(os.getenv("EVIDENCE_TOKEN_BUDGET", "12000"))),
        evidence_catalog_token_budget=max(200, int(os.getenv("EVIDENCE_CATALOG_TOKEN_BUDGET", "1500"))),
        evidence_max_fragments=max(1, int(os.getenv("EVIDENCE_MAX_FRAGMENTS", "30"))),
        evidence_top_k_per_field=max(1, int(os.getenv("EVIDENCE_TOP_K_PER_FIELD", "5"))),
        qwen_provider_profile=qwen_profile,
    )


def _resolve_qwen_connection(
    *, model: str = "qwen3.8-max", desktop_config: dict[str, Any], qwen_config: dict[str, Any]
) -> tuple[str, str | None, str]:
    """Resolve an auditable model endpoint without putting credentials in YAML.

    ``auto`` routes Qwen to DashScope, DeepSeek to its official API, and every
    other model to N1N. Explicit profiles remain available for controlled
    comparison experiments. ``legacy`` preserves QWEN_API_KEY/QWEN_BASE_URL.
    """

    requested = os.getenv("QWEN_PROVIDER_PROFILE", "auto").strip().lower() or "auto"
    profile = _profile_for_model(model, requested)
    if profile == "official":
        key = _as_optional_str(os.getenv("QWEN_OFFICIAL_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
        base_url = os.getenv(
            "QWEN_OFFICIAL_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    elif profile == "deepseek_official":
        key = _as_optional_str(os.getenv("DEEPSEEK_OFFICIAL_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))
        base_url = os.getenv("DEEPSEEK_OFFICIAL_BASE_URL", "https://api.deepseek.com")
    elif profile == "n1n":
        key = _as_optional_str(
            os.getenv("QWEN_N1N_API_KEY") or os.getenv("N1N_API_KEY") or os.getenv("QWEN_API_KEY")
        )
        base_url = os.getenv("QWEN_N1N_BASE_URL", "https://api.n1n.ai/v1")
    elif profile == "legacy":
        key = _as_optional_str(os.getenv("QWEN_API_KEY"))
        base_url = os.getenv(
            "QWEN_BASE_URL",
            _as_str(
                desktop_config.get("qwen_base_url"),
                _as_str(qwen_config.get("base_url"), "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ),
        )
    else:
        raise ValueError(
            "QWEN_PROVIDER_PROFILE must be one of: auto, legacy, official, deepseek_official, deepseek, n1n"
        )
    return profile, key, str(base_url).strip().rstrip("/")


def _profile_for_model(model: str, requested_profile: str = "auto") -> str:
    requested = requested_profile.strip().lower() or "auto"
    if requested == "deepseek":
        return "deepseek_official"
    if requested != "auto":
        return requested
    model_id = model.strip().lower()
    if model_id.startswith(("qwen", "qwq")):
        return "official"
    if model_id.startswith("deepseek"):
        return "deepseek_official"
    return "n1n"


def resolve_model_connection(model: str, settings: Settings) -> tuple[str, str | None, str]:
    """Resolve a role-specific model when engineering/FEM models differ."""

    requested = os.getenv("QWEN_PROVIDER_PROFILE", "auto").strip().lower() or "auto"
    profile = _profile_for_model(model, requested)
    loaded_profile = getattr(settings, "qwen_provider_profile", "legacy")
    if profile == "official":
        return profile, _as_optional_str(
            os.getenv("QWEN_OFFICIAL_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or (settings.qwen_api_key if loaded_profile == profile else None)
        ), os.getenv("QWEN_OFFICIAL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    if profile == "deepseek_official":
        return profile, _as_optional_str(
            os.getenv("DEEPSEEK_OFFICIAL_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or (settings.qwen_api_key if loaded_profile == profile else None)
        ), os.getenv("DEEPSEEK_OFFICIAL_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if profile == "n1n":
        return profile, _as_optional_str(
            os.getenv("QWEN_N1N_API_KEY")
            or os.getenv("N1N_API_KEY")
            or os.getenv("QWEN_API_KEY")
            or (settings.qwen_api_key if loaded_profile == profile else None)
        ), os.getenv("QWEN_N1N_BASE_URL", "https://api.n1n.ai/v1").rstrip("/")
    if profile == "legacy":
        return profile, settings.qwen_api_key, settings.qwen_base_url
    raise ValueError(
        "QWEN_PROVIDER_PROFILE must be one of: auto, legacy, official, deepseek_official, deepseek, n1n"
    )


def ensure_runtime_directories() -> None:
    """Create only application-owned runtime roots.

    In browser/development mode this preserves the historical source-tree
    layout. Desktop launchers inject per-user roots before importing the app.
    """

    for path in (
        STORAGE_ROOT,
        INPUT_BATCH_ROOT,
        ABAQUSWORK,
        LOG_ROOT,
        CACHE_ROOT,
        CONFIG_ROOT,
        BACKUP_ROOT,
        KNOWLEDGE_ROOT,
        MODEL_LIBRARY_ROOT,
    ):
        path.mkdir(parents=True, exist_ok=True)


def load_desktop_settings() -> dict[str, Any]:
    if not DESKTOP_SETTINGS_FILE.exists():
        return {}
    try:
        payload = json.loads(DESKTOP_SETTINGS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save_desktop_settings(payload: dict[str, Any]) -> None:
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = DESKTOP_SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(DESKTOP_SETTINGS_FILE)


def load_llm_api_config(path: Path = LLM_API_CONFIG) -> dict[str, Any]:
    """Load non-secret LLM options without adding a PyYAML dependency."""
    if not path.exists():
        return {}
    root: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.endswith(":"):
            section = line[:-1].strip()
            root[section] = {}
            current_section = root[section]
            continue
        if current_section is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_section[key.strip()] = _parse_yaml_scalar(value.strip())
    return root


def _parse_yaml_scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _as_str(value: Any, default: str) -> str:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _as_optional_str(value: Any) -> str | None:
    text = _as_str(value, "")
    if text.startswith("${") and text.endswith("}"):
        return os.getenv(text[2:-1]) or None
    return text or None
