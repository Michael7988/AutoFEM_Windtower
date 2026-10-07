from __future__ import annotations

from windtower_backend.app import create_app


REQUIRED_ROUTES = {
    ("GET", "/api/health"),
    ("GET", "/api/capabilities"),
    ("GET", "/api/desktop/health"),
    ("GET", "/api/desktop/settings"),
    ("PUT", "/api/desktop/settings"),
    ("POST", "/api/desktop/settings/llm-key"),
    ("POST", "/api/desktop/data/import"),
    ("GET", "/api/desktop/shutdown-readiness"),
    ("GET", "/api/desktop/version"),
    ("GET", "/api/llm/capabilities"),
    ("POST", "/api/llm/connection-test"),
    ("GET", "/api/modeling-capabilities/current"),
    ("GET", "/api/knowledge-bases/status"),
    ("GET", "/api/model-library"),
    ("POST", "/api/input-batches"),
    ("GET", "/api/input-batches/{batch_id}"),
    ("PUT", "/api/input-batches/{batch_id}/reference-model"),
    ("DELETE", "/api/input-batches/{batch_id}/reference-model"),
    ("POST", "/api/input-batches/{batch_id}/reference-model/select"),
    ("POST", "/api/input-batches/{batch_id}/engineering-review/confirm"),
    ("POST", "/api/input-batches/{batch_id}/fem-specification/confirm"),
    ("POST", "/api/input-batches/{batch_id}/template/confirm"),
    ("POST", "/api/input-batches/{batch_id}/intent/confirm"),
    ("POST", "/api/input-batches/{batch_id}/conflicts/resolve"),
    ("POST", "/api/input-batches/{batch_id}/retry"),
    ("POST", "/api/input-batches/{batch_id}/parameters/complete"),
    ("POST", "/api/input-batches/{batch_id}/files"),
    ("POST", "/api/input-batches/{batch_id}/reparse"),
    ("POST", "/api/input-batches/{batch_id}/confirm"),
    ("GET", "/api/projects/{project_id}"),
    ("GET", "/api/projects/latest"),
    ("GET", "/api/projects/{project_id}/parameters"),
    ("POST", "/api/projects/{project_id}/parameter-changes/preview"),
    ("POST", "/api/projects/{project_id}/parameter-changes/confirm"),
    ("POST", "/api/projects/{project_id}/validate"),
    ("POST", "/api/projects/{project_id}/mesh"),
    ("POST", "/api/projects/{project_id}/mesh-preview"),
    ("POST", "/api/projects/{project_id}/build"),
    ("GET", "/api/projects/{project_id}/workflow-state"),
    ("GET", "/api/projects/{project_id}/fe-semantic-ir"),
    ("GET", "/api/projects/{project_id}/dependency-report"),
    ("POST", "/api/projects/{project_id}/semantic/preview"),
    ("POST", "/api/projects/{project_id}/repairs/preview"),
    ("POST", "/api/projects/{project_id}/datacheck"),
    ("POST", "/api/projects/{project_id}/submissions"),
    ("GET", "/api/projects/{project_id}/submissions/{submission_id}"),
    ("POST", "/api/projects/{project_id}/postprocess"),
    ("GET", "/api/projects/{project_id}/results"),
    ("GET", "/api/projects/{project_id}/result-mesh"),
    ("GET", "/api/projects/{project_id}/artifacts"),
    ("GET", "/api/projects/{project_id}/artifacts/{name:path}"),
    ("POST", "/api/projects/{project_id}/model-library/publish"),
}

REMOVED_PATH_FRAGMENTS = {
    "/agent/repair",
    "/knowledge/",
    "/batch/four-cases",
    "/llm/patch",
    "/patches/apply",
    "/compiler-readiness",
    "/l0/",
}


def test_public_routes_match_github_v1_contract() -> None:
    app = create_app()
    routes = {
        (method, route.path)
        for route in app.routes
        if route.path.startswith("/api/")
        for method in (route.methods or set())
    }
    assert REQUIRED_ROUTES <= routes
    assert len(routes) == len(set(routes))
    assert not [
        path
        for _method, path in routes
        if any(fragment in path for fragment in REMOVED_PATH_FRAGMENTS)
    ]
