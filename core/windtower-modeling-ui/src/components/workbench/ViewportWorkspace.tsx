import { Eye, Grid3X3, RefreshCw, Rotate3D } from "lucide-react";

import type {
  HighlightMode,
  MeshReport,
  OdbResultMesh,
  PyVistaPreview,
  ResultScalar,
  ViewMode,
} from "../../types";
import { ModelViewport } from "../viewport/ModelViewport";

export type ViewContent = "mesh" | "result";

interface ViewportWorkspaceProps {
  busy: boolean;
  deformationScale: number;
  highlight: HighlightMode;
  mesh: MeshReport | null;
  meshDataUrl: string;
  meshPreview: PyVistaPreview | null;
  onDeformationScaleChange: (value: number) => void;
  onError: (reason: unknown) => void;
  onHighlightChange: (value: HighlightMode) => void;
  onRefresh: () => void;
  onScalarChange: (value: ResultScalar) => void;
  onViewContentChange: (value: ViewContent) => void;
  onViewModeChange: (value: ViewMode) => void;
  projectReady: boolean;
  resultMesh: OdbResultMesh | null;
  scalar: ResultScalar;
  viewContent: ViewContent;
  viewMode: ViewMode;
}

export function ViewportWorkspace({
  busy,
  deformationScale,
  highlight,
  mesh,
  meshDataUrl,
  meshPreview,
  onDeformationScaleChange,
  onError,
  onHighlightChange,
  onRefresh,
  onScalarChange,
  onViewContentChange,
  onViewModeChange,
  projectReady,
  resultMesh,
  scalar,
  viewContent,
  viewMode,
}: ViewportWorkspaceProps) {
  return (
    <section className="viewport-panel">
      <div className="viewport-toolbar">
        <div className="segmented">
          <button className={viewContent === "mesh" ? "active" : ""} onClick={() => onViewContentChange("mesh")} type="button">
            <Grid3X3 size={14} />网格
          </button>
          <button className={viewContent === "result" ? "active" : ""} disabled={!resultMesh} onClick={() => onViewContentChange("result")} type="button">
            <Eye size={14} />ODB 结果
          </button>
        </div>
        <div className="toolbar-divider" />
        <div className="view-buttons" aria-label="网格视角">
          {([[
            "3d", "三维",
          ], [
            "xz", "正视",
          ], [
            "yz", "侧视",
          ], [
            "xy", "俯视",
          ]] as Array<[ViewMode, string]>).map(([value, label]) => (
            <button className={viewMode === value ? "active" : ""} key={value} onClick={() => onViewModeChange(value)} type="button">
              {value === "3d" ? <Rotate3D size={14} /> : null}{label}
            </button>
          ))}
        </div>
        <div className="toolbar-spacer" />
        {viewContent === "mesh" ? (
          <div className="segmented compact">
            <button className={highlight === "mesh" ? "active" : ""} onClick={() => onHighlightChange("mesh")} type="button">整体网格</button>
            {meshPreview?.feature_sets?.bolt_elements ? (
              <>
                <button className={highlight === "flange" ? "active" : ""} onClick={() => onHighlightChange("flange")} type="button">法兰</button>
                <button className={highlight === "contact" ? "active" : ""} onClick={() => onHighlightChange("contact")} type="button">接触面</button>
                <button className={highlight === "bolts" ? "active" : ""} onClick={() => onHighlightChange("bolts")} type="button">螺栓</button>
              </>
            ) : (
              <button className={highlight === "door" ? "active" : ""} onClick={() => onHighlightChange("door")} type="button">门洞局部</button>
            )}
          </div>
        ) : (
          <>
            <select className="toolbar-select" value={scalar} onChange={(event) => onScalarChange(event.target.value as ResultScalar)}>
              <option value="S_Mises">Mises 应力</option>
              <option value="U_mag">位移 U</option>
            </select>
            <label className="scale-control">
              变形 ×
              <input min="0" max="50" step="1" type="number" value={deformationScale} onChange={(event) => onDeformationScaleChange(Number(event.target.value))} />
            </label>
          </>
        )}
      </div>
      <div className="viewport-canvas">
        <ModelViewport
          deformationScale={deformationScale}
          highlight={highlight}
          meshDataUrl={meshDataUrl}
          mode={viewContent}
          onError={onError}
          onViewModeChange={onViewModeChange}
          resultMesh={resultMesh}
          scalar={scalar}
          sourceLabel={viewContent === "result" ? "结果云图" : "网格预览"}
          viewMode={viewMode}
          warnings={viewContent === "result" ? resultMesh?.warnings : meshPreview?.warnings}
        />
      </div>
      <div className="viewport-footer">
        <MeshMetric label="节点" value={meshCount(meshPreview, mesh, "nodes")} />
        <MeshMetric label="S4R 单元" value={meshCount(meshPreview, mesh, "s4r_elements")} />
        {meshPreview?.feature_sets?.bolt_elements ? <MeshMetric label="B31 螺栓" value={meshCount(meshPreview, mesh, "b31_elements")} accent /> : null}
        <MeshMetric label="门洞单元" value={meshCount(meshPreview, mesh, "door_patch_elements")} accent />
        <div className="quality-indicator">
          <span className={`quality-dot ${mesh?.status === "pass" || mesh?.status === "ready" ? "pass" : "idle"}`} />
          {mesh?.warnings?.length ? `${mesh.warnings.length} 项网格警告` : "等待网格质量检查"}
        </div>
        <button className="ghost-button compact-button" disabled={!projectReady || busy} onClick={onRefresh} type="button">
          <RefreshCw size={14} />刷新预览
        </button>
      </div>
    </section>
  );
}

function MeshMetric({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className={accent ? "mesh-metric accent" : "mesh-metric"}>
      <span>{label}</span><strong>{value}</strong>
    </div>
  );
}

function meshCount(preview: PyVistaPreview | null, mesh: MeshReport | null, key: string) {
  const value = preview?.mesh_stats?.[key] ?? mesh?.mesh_stats?.[key];
  return typeof value === "number" ? value.toLocaleString() : "—";
}
