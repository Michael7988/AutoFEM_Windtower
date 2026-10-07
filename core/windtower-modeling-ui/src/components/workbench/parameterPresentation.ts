import type { InputBatch } from "../../types";

const LEGACY_FIXED_FEM_PARAMETERS = new Set([
  "mesh.strategy",
  "mesh.strategy_family",
  "mesh.element_type",
]);

export function isFemParameterEditable(batch: InputBatch | null, key: string) {
  const configured = batch?.fem_specification?.editable_parameter_keys;
  return configured ? configured.includes(key) : !LEGACY_FIXED_FEM_PARAMETERS.has(key);
}

export function equivalent(first: unknown, second: unknown) {
  if (typeof first === "number" || typeof second === "number") {
    return Number(first) === Number(second);
  }
  return JSON.stringify(first) === JSON.stringify(second);
}

export function coerceDraft(value: string, original: unknown) {
  if (typeof original === "number") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : value;
  }
  return value;
}

export function originLabel(origin: string) {
  const labels: Record<string, string> = {
    engineering_evidence: "工程资料",
    fem_evidence: "FEM 资料",
    fem_file: "源数据",
    reference_model: "参考模型",
    capability_default: "能力配置",
    engineer_input: "工程师输入",
    llm_suggestion: "LLM 建议",
    derived_rule: "规则派生",
    model_library: "已验证模型",
  };
  return labels[origin] ?? origin;
}

export function femSourceBranchLabel(branch: string) {
  return ({
    source_data: "源数据",
    engineer_input: "工程师输入",
    reference_model: "参考模型",
    llm_recommendation: "LLM 推荐",
    capability_profile: "能力配置",
  } as Record<string, string>)[branch] ?? branch;
}

export function shortFemLabel(key: string) {
  return safeParameterLabel(key, key);
}

export function candidateForReview(batch: InputBatch, key: string, value: unknown) {
  return batch.candidates.find((candidate) => (
    candidate.key === key && equivalent(candidate.normalized_value, value)
  )) ?? batch.candidates.find((candidate) => candidate.key === key);
}

export function candidateSourceLabel(sourceKind?: string, extractionMethod?: string) {
  const labels: Record<string, string> = {
    file_observed: "工程观测",
    engineer_input: "工程师输入",
    reference_model_value: "参考模型",
    model_library_reference: "参考项目值",
    llm_mapped: "LLM 映射",
    llm_suggested: "LLM 建议",
    system_default: "能力默认",
    system_setting: "待工程师补充",
  };
  return labels[sourceKind ?? ""] ?? (extractionMethod === "deterministic_derived" ? "规则派生" : "待确认");
}

export function isMissingValue(value: unknown) {
  return value === null || value === undefined || value === "";
}

export function safeParameterLabel(key: string, fallback: string) {
  const labels: Record<string, string> = {
    "geometry.door.enabled": "启用门洞",
    "geometry.door.width_mm": "门洞宽度",
    "geometry.door.height_mm": "门洞高度",
    "geometry.door.bottom_z_mm": "门洞底标高",
    "geometry.door.theta_center_deg": "门洞环向角度",
    "material.name": "材料名称",
    "material.elastic_modulus_mpa": "弹性模量",
    "material.poisson_ratio": "泊松比",
    "material.density": "材料密度",
    "material.yield_strength_mpa": "屈服强度",
    "mesh.element_type": "壳单元类型",
    "mesh.global_size_mm": "全局网格尺寸",
    "mesh.door_size_mm": "门洞局部网格尺寸",
    "mesh.patch_margin_u_mm": "门洞环向细化边界",
    "mesh.patch_margin_z_mm": "门洞高度向细化边界",
    "mesh.hole_layers": "门洞环向层数",
    "loads.top_mass_tonne": "顶部等效质量",
    "loads.top_horizontal_force_n": "顶部水平力",
    "loads.top_force_direction": "顶部力方向",
    "loads.wind_pressure_mpa": "风压",
    "analysis.static_enabled": "静力分析",
    "analysis.modal_enabled": "模态分析",
    "analysis.modal_modes": "模态阶数",
    "boundary.name": "底部边界条件",
  };
  if (labels[key]) return labels[key];
  const tower = key.match(/^geometry\.tower_sections\.(\d+)\.(.+)$/);
  if (tower) {
    const names: Record<string, string> = {
      z_bottom_mm: "底标高",
      z_top_mm: "顶标高",
      diameter_bottom_mm: "底直径",
      diameter_top_mm: "顶直径",
      thickness_mm: "厚度",
    };
    return `第 ${Number(tower[1]) + 1} 段${names[tower[2]] ?? tower[2]}`;
  }
  return fallback || key;
}
