import { AlertTriangle, Box, Check } from "lucide-react";

import type { InputBatch, ParameterRecord } from "../../types";
import {
  candidateForReview,
  candidateSourceLabel,
  coerceDraft,
  isMissingValue,
  isFemParameterEditable,
  originLabel,
  femSourceBranchLabel,
  safeParameterLabel,
  shortFemLabel,
} from "./parameterPresentation";

export type PreparationDomain = "engineering" | "fem";

const GROUPS: Array<{ key: ParameterRecord["group"]; label: string }> = [
  { key: "geometry", label: "几何与门洞" },
  { key: "material", label: "材料截面" },
  { key: "interaction", label: "接触与预紧" },
  { key: "mesh", label: "网格" },
  { key: "loads", label: "荷载" },
  { key: "boundary", label: "边界" },
  { key: "analysis", label: "分析任务" },
];

export function ParameterEditor({
  rows,
  groupedTowerRows,
  draftValues,
  setDraftValue,
}: {
  rows: ParameterRecord[];
  groupedTowerRows: Array<{ index: number; rows: ParameterRecord[] }>;
  draftValues: Record<string, unknown>;
  setDraftValue: (key: string, value: unknown) => void;
}) {
  const domains: Array<{ key: ParameterRecord["domain"]; label: string; note: string }> = [
    { key: "engineering", label: "工程参数", note: "来自结构资料与工程师确认" },
    { key: "fem", label: "FEM 参数", note: "来自参考规范与能力配置" },
  ];
  return (
    <div className="parameter-editor">
      {domains.map((domain) => (
        <section className="parameter-domain" key={domain.key}>
          <div className="parameter-domain-heading"><strong>{domain.label}</strong><span>{domain.note}</span></div>
          {domain.key === "engineering" && groupedTowerRows.length ? (
            <details open>
              <summary>塔段参数 <small>{groupedTowerRows.length} 段 · {originLabel(groupedTowerRows[0].rows[0]?.origin ?? "engineering_evidence")}</small></summary>
              <div className="tower-table-wrap">
                <table className="tower-table">
                  <thead><tr><th>段</th><th>底标高</th><th>顶标高</th><th>底直径</th><th>顶直径</th><th>厚度</th></tr></thead>
                  <tbody>{groupedTowerRows.map((section) => (
                    <tr key={section.index}>
                      <th>S{section.index + 1}</th>
                      {["z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm"].map((field) => {
                        const row = section.rows.find((item) => item.key.endsWith(`.${field}`));
                        return <td key={field}>{row ? (
                          <input aria-label={safeParameterLabel(row.key, row.label)} value={String(draftValues[row.key] ?? "")} onChange={(event) => setDraftValue(row.key, coerceDraft(event.target.value, row.value))} />
                        ) : "—"}</td>;
                      })}
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </details>
          ) : null}
          {GROUPS.map((group) => {
            const groupRows = rows.filter((row) => row.domain === domain.key && row.group === group.key && !row.key.startsWith("geometry.tower_sections."));
            if (!groupRows.length) return null;
            return (
              <details key={`${domain.key}-${group.key}`} open={group.key === "geometry" || group.key === "mesh"}>
                <summary>{group.label}<small>{groupRows.length} 项</small></summary>
                <div className="parameter-form-grid">{groupRows.map((row) => (
                  <label key={row.key}>
                    <span>{safeParameterLabel(row.key, row.label)}{row.unit ? <em>{row.unit}</em> : null}<small className="origin-badge">{originLabel(row.origin)}</small></span>
                    {typeof row.value === "boolean" ? (
                      <select value={String(Boolean(draftValues[row.key]))} onChange={(event) => setDraftValue(row.key, event.target.value === "true")}><option value="true">启用</option><option value="false">禁用</option></select>
                    ) : (
                      <input value={String(draftValues[row.key] ?? "")} onChange={(event) => setDraftValue(row.key, coerceDraft(event.target.value, row.value))} />
                    )}
                  </label>
                ))}</div>
              </details>
            );
          })}
        </section>
      ))}
    </div>
  );
}

export function PreparationParameterReview({
  batch,
  domain,
  engineeringDraftValues,
  femDraftValues,
  setEngineeringDraftValue,
  setFemDraftValue,
}: {
  batch: InputBatch | null;
  domain: PreparationDomain;
  engineeringDraftValues: Record<string, unknown>;
  femDraftValues: Record<string, unknown>;
  setEngineeringDraftValue: (key: string, value: unknown) => void;
  setFemDraftValue: (key: string, value: unknown) => void;
}) {
  if (!batch) return <EmptyPanel title="等待输入资料" text="上传工程资料后，两个顾问分别形成工程参数与 FEM 规范候选。" />;
  if (domain === "engineering") {
    const rows = [...(batch.review_parameters ?? [])].sort((left, right) => Number(isMissingValue(engineeringDraftValues[left.key] ?? left.value)) * -1 - Number(isMissingValue(engineeringDraftValues[right.key] ?? right.value)) * -1);
    if (!rows.length && batch.capability_id === "WT-FLANGE-BOLT-CONTACT-PRELOAD-V1") {
      return <EmptyPanel title="L2 工程目标已接收" text="当前局部法兰能力没有独立工程参数表；请在 FEM 参数页确认塔节、法兰、螺栓、接触、预紧和边界候选。" />;
    }
    if (!rows.length) return <EmptyPanel title="工程参数尚未就绪" text="等待工程资料提取完成，缺失值将保留为空并由工程师补充。" />;
    return (
      <div className="preparation-review">
        <ReviewIntroduction confirmed={batch.engineering_review?.status === "confirmed"} text="工程顾问只映射带来源的工程事实；参考项目值和 LLM 建议不会自动写入。" title="工程参数候选" />
        <div className="preparation-parameter-list">{rows.map((row) => {
          const source = candidateForReview(batch, row.key, row.value);
          const current = engineeringDraftValues[row.key] ?? row.value;
          return (
            <label className={isMissingValue(current) ? "missing-parameter" : ""} key={row.key}>
              <span><strong>{safeParameterLabel(row.key, row.key)}</strong><em>{candidateSourceLabel(source?.source_kind, source?.extraction_method)}</em></span>
              <div>{typeof row.value === "boolean" ? (
                <select disabled={batch.engineering_review?.status === "confirmed"} value={String(Boolean(current))} onChange={(event) => setEngineeringDraftValue(row.key, event.target.value === "true")}><option value="true">启用</option><option value="false">禁用</option></select>
              ) : (
                <input disabled={batch.engineering_review?.status === "confirmed"} value={String(current ?? "")} onChange={(event) => setEngineeringDraftValue(row.key, coerceDraft(event.target.value, row.value))} />
              )}<small>{row.unit ?? ""}</small></div>
            </label>
          );
        })}</div>
      </div>
    );
  }
  const parameters = batch.fem_specification?.parameters ?? {};
  if (!Object.keys(parameters).length) return <EmptyPanel title="FEM 规范尚未就绪" text="请选择模型库、上传参考 INP，或使用当前能力配置。" />;
  return (
    <div className="preparation-review">
      <ReviewIntroduction confirmed={batch.fem_review?.status === "confirmed"} text="FEM 顾问解释建模规范；网格拓扑、Abaqus 关键词和 INP 均由确定性生成器负责。" title="FEM 参数候选" />
      <TransferReview transfer={batch.reference_transfer} />
      <div className="preparation-parameter-list">{Object.entries(parameters).sort(([leftKey, left], [rightKey, right]) => Number(isMissingValue(femDraftValues[leftKey] ?? left.value)) * -1 - Number(isMissingValue(femDraftValues[rightKey] ?? right.value)) * -1).map(([key, item]) => (
        <label className={isMissingValue(femDraftValues[key] ?? item.value) ? "missing-parameter" : ""} key={key}>
          <span><strong>{shortFemLabel(key)}</strong><em>{isFemParameterEditable(batch, key) ? originLabel(item.origin) : `${originLabel(item.origin)} · 固定规则`}</em></span>
          <div>{typeof item.value === "boolean" ? (
            <select disabled={!isFemParameterEditable(batch, key) || batch.fem_review?.status === "confirmed"} value={String(Boolean(femDraftValues[key]))} onChange={(event) => setFemDraftValue(key, event.target.value === "true")}><option value="true">启用</option><option value="false">禁用</option></select>
          ) : (
            <input disabled={!isFemParameterEditable(batch, key) || batch.fem_review?.status === "confirmed"} value={String(femDraftValues[key] ?? "")} onChange={(event) => setFemDraftValue(key, coerceDraft(event.target.value, item.value))} />
          )}</div>
          {item.alternatives?.length ? <small className="alternative-note">另有 {item.alternatives.length} 个来源候选可追溯</small> : null}
          {item.sources?.length ? (
            <small className="alternative-note">
              证据链：{Array.from(new Set(item.sources.map((source) => femSourceBranchLabel(source.branch)))).join(" → ")}
            </small>
          ) : null}
        </label>
      ))}</div>
    </div>
  );
}

function EmptyPanel({ title, text }: { title: string; text: string }) {
  return <div className="empty-panel"><Box size={28} /><strong>{title}</strong><span>{text}</span></div>;
}

function TransferReview({ transfer }: { transfer?: InputBatch["reference_transfer"] }) {
  if (!transfer) return null;
  const reusable = transfer.reusable ?? [];
  const conditional = transfer.conditional ?? [];
  const deferred = transfer.deferred_features ?? [];
  if (!reusable.length && !conditional.length && !deferred.length) return null;
  return (
    <div className="transfer-review">
      {reusable.length ? <section><strong>可复用规范</strong>{reusable.map((item) => <p key={`${item.module}-${item.label}`}><Check size={13} />{item.label}</p>)}</section> : null}
      {conditional.length ? <section className="conditional"><strong>需工程师确认</strong>{conditional.map((item) => <p key={`${item.module}-${item.label}`} title={item.reason}><AlertTriangle size={13} />{item.label}</p>)}</section> : null}
      {deferred.length ? <details><summary>当前不处理（{deferred.length}）</summary>{deferred.map((item) => <p key={`${item.module}-${item.label}`}>{item.label}：{item.reason}</p>)}</details> : null}
    </div>
  );
}

function ReviewIntroduction({ title, text, confirmed }: { title: string; text: string; confirmed: boolean }) {
  return <div className="review-introduction"><div><strong>{title}</strong><span className={`state-badge ${confirmed ? "pass" : "idle"}`}>{confirmed ? "已确认" : "待确认"}</span></div><p>{text}</p></div>;
}
