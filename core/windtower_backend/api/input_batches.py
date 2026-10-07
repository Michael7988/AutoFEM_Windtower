from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from ..input_batches.models import (
    CompleteParametersRequest,
    ConfirmAndCreateRequest,
    ConfirmBatchReviewRequest,
    ConfirmFemSpecificationRequest,
    ConfirmIntentRequest,
    ConfirmTemplateRequest,
    ReparseRequest,
    ResolveConflictsRequest,
)
from pydantic import BaseModel
from ..input_batches.service import EngineeringInputBatchService
from ..input_batches.extractors import validate_engineering_upload
from .uploads import (
    SpooledUpload,
    UploadLimitError,
    cleanup_spooled_uploads,
    spool_uploads,
)


class SelectLibraryModelRequest(BaseModel):
    model_id: str


def create_input_batches_router(service: EngineeringInputBatchService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/llm/capabilities")
    def llm_capabilities() -> dict[str, object]:
        return service.capabilities()

    @router.post("/llm/connection-test")
    def test_llm_connection() -> dict[str, object]:
        return service.connection_test()

    @router.get("/modeling-capabilities/current")
    def current_modeling_capability() -> dict[str, object]:
        return service.modeling_capability()

    @router.get("/knowledge-bases/status")
    def knowledge_bases_status() -> dict[str, object]:
        return service.knowledge_status()

    @router.post("/input-batches", status_code=202)
    async def create_input_batch(
        files: list[UploadFile] | None = File(None),
        reference_model: UploadFile | None = File(None),
        modeling_goal: str | None = Form(None),
        parse_mode: str = Form("llm"),
        llm_execution_mode: str = Form("single"),
        model_library_id: str | None = Form(None),
        capability_id: str = Form("WT-SHELL-DOOR-STATIC-MODAL-V1"),
    ) -> dict[str, object]:
        temp_root = None
        try:
            if not (modeling_goal or "").strip():
                raise ValueError("请填写建模目标。建模目标是项目准备流程的必填输入。")
            engineering_files = list(files or [])
            incoming = [*engineering_files]
            if reference_model is not None:
                incoming.append(reference_model)
            stored: list[SpooledUpload] = []
            if incoming:
                temp_root, stored = await spool_uploads(incoming, service.pipeline.settings)
            engineering_count = len(engineering_files)
            uploads = [
                (item.filename, item.media_type, item.path)
                for item in stored[:engineering_count]
            ]
            for filename, _media_type, path in uploads:
                validate_engineering_upload(filename, path.read_bytes())
            reference = None
            if reference_model is not None:
                item = stored[-1]
                reference = (item.filename, item.path)
            return service.create_batch_from_paths(
                uploads,
                modeling_goal=modeling_goal,
                parse_mode=parse_mode,
                llm_execution_mode=llm_execution_mode,
                reference_model=reference,
                model_library_id=model_library_id,
                capability_id=capability_id,
            )
        except (ValueError, UploadLimitError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            if temp_root is not None:
                cleanup_spooled_uploads(temp_root)

    @router.get("/input-batches/{batch_id}")
    def get_input_batch(batch_id: str) -> dict[str, object]:
        try:
            return service.get_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc

    @router.get("/input-batches/{batch_id}/documents")
    def list_input_documents(batch_id: str) -> dict[str, object]:
        try:
            return service.list_documents(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc

    @router.get("/input-batches/{batch_id}/documents/{file_id}/markdown", response_class=PlainTextResponse)
    def get_input_document_markdown(batch_id: str, file_id: str) -> str:
        try:
            return service.get_document_markdown(batch_id, file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="找不到该文档Markdown") from exc

    @router.get("/input-batches/{batch_id}/evidence")
    def query_input_evidence(
        batch_id: str,
        evidence_id: str | None = None,
        field: str | None = None,
        status: str | None = None,
        file_id: str | None = None,
        location: str | None = None,
    ) -> dict[str, object]:
        try:
            return service.query_evidence(
                batch_id,
                evidence_id=evidence_id,
                field=field,
                status=status,
                file_id=file_id,
                location=location,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc

    @router.put("/input-batches/{batch_id}/reference-model", status_code=202)
    async def put_reference_model(
        batch_id: str,
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        temp_root = None
        try:
            temp_root, stored = await spool_uploads([file], service.pipeline.settings)
            item = stored[0]
            return service.put_reference_model_from_path(
                batch_id,
                item.filename,
                item.path,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except (ValueError, UploadLimitError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail="参考模型解析失败，请检查 INP 编码、关键词结构和文件完整性。",
            ) from exc
        finally:
            if temp_root is not None:
                cleanup_spooled_uploads(temp_root)

    @router.delete("/input-batches/{batch_id}/reference-model")
    def delete_reference_model(batch_id: str) -> dict[str, object]:
        try:
            return service.delete_reference_model(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/reference-model/select")
    def select_reference_model(
        batch_id: str,
        request: SelectLibraryModelRequest,
    ) -> dict[str, object]:
        try:
            return service.select_library_model(batch_id, request.model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到批次或模型库版本") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/fem-specification/confirm")
    def confirm_fem_specification(
        batch_id: str,
        request: ConfirmFemSpecificationRequest,
    ) -> dict[str, object]:
        try:
            return service.confirm_fem_specification(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/template/confirm", status_code=202)
    def confirm_input_batch_template(batch_id: str, request: ConfirmTemplateRequest) -> dict[str, object]:
        try:
            return service.confirm_template(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/intent/confirm")
    def confirm_input_batch_intent(batch_id: str, request: ConfirmIntentRequest) -> dict[str, object]:
        try:
            return service.confirm_intent(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/llm-retry", status_code=202)
    def retry_input_batch_llm(batch_id: str) -> dict[str, object]:
        try:
            return service.retry_llm(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/conflicts/resolve")
    def resolve_input_batch_conflicts(batch_id: str, request: ResolveConflictsRequest) -> dict[str, object]:
        try:
            return service.resolve_conflicts(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/retry", status_code=202)
    def retry_input_batch(batch_id: str) -> dict[str, object]:
        try:
            return service.retry(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc

    @router.post("/input-batches/{batch_id}/parameters/complete")
    def complete_input_batch_parameters(batch_id: str, request: CompleteParametersRequest) -> dict[str, object]:
        try:
            return service.complete_parameters(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/files", status_code=202)
    async def append_input_batch_files(batch_id: str, files: list[UploadFile] = File(...)) -> dict[str, object]:
        temp_root = None
        try:
            temp_root, stored = await spool_uploads(files, service.pipeline.settings)
            uploads = [(item.filename, item.media_type, item.path) for item in stored]
            for filename, _media_type, path in uploads:
                validate_engineering_upload(filename, path.read_bytes())
            return service.append_files_from_paths(batch_id, uploads)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except (ValueError, UploadLimitError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if temp_root is not None:
                cleanup_spooled_uploads(temp_root)

    @router.post("/input-batches/{batch_id}/reparse", status_code=202)
    def reparse_input_batch(batch_id: str, request: ReparseRequest) -> dict[str, object]:
        try:
            return service.reparse(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/confirm")
    def confirm_input_batch(
        batch_id: str,
        request: ConfirmBatchReviewRequest | None = None,
    ) -> dict[str, object]:
        try:
            return service.confirm(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/confirm-and-create")
    def confirm_and_create_project(
        batch_id: str,
        request: ConfirmAndCreateRequest,
    ) -> dict[str, object]:
        try:
            return service.confirm_and_create(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/input-batches/{batch_id}/engineering-review/confirm")
    def confirm_engineering_review(
        batch_id: str,
        request: ConfirmBatchReviewRequest,
    ) -> dict[str, object]:
        try:
            return service.confirm_engineering_review(batch_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到该解析批次") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
