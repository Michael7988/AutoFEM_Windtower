import { Languages } from "lucide-react";
import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type Language = "zh-CN" | "en-US";

const STORAGE_KEY = "windtower.language";

const ENGLISH: Record<string, string> = {
  "LLM 辅助参数化有限元建模工作台": "LLM-Assisted Parametric Finite Element Modeling Workbench",
  "风电塔筒 CAE 建模工作台": "Wind Tower CAE Modeling Workbench",
  "新建验证": "New Validation",
  "恢复最近项目": "Restore Recent Project",
  "打开输入栏": "Open Input Panel",
  "打开参数栏": "Open Parameter Panel",
  "关闭": "Close",
  "语言切换": "Language selector",
  "输入资料": "Input Data",
  "确认参数": "Parameter Review",
  "网格与模型": "Mesh & Model",
  "模型检查": "Model Check",
  "求解结果": "Solver Results",
  "建模工作流": "Modeling Workflow",
  "待执行": "Not Started",
  "可执行": "Ready",
  "执行中": "Running",
  "已阻断": "Blocked",
  "已完成": "Completed",
  "输入与解析": "Input & Parsing",
  "已完成 · 输入资料": "Completed · Input Data",
  "已建立正式参数集": "Official parameter set created",
  "建模能力": "Modeling Capability",
  "建模目标": "Modeling Objective",
  "带门洞整塔壳模型": "Full-tower shell model with door opening",
  "局部法兰螺栓接触预紧": "Local flange–bolt contact and preload",
  "范围固定为局部双塔节、单法兰、B31 螺栓与三步预紧；不支持实体螺栓或多法兰。": "Scope is limited to two local tower sections, one flange, B31 bolts, and a three-step preload sequence; solid bolts and multiple flanges are not supported.",
  "带门洞整塔 S4R 壳模型，支持线性静力与模态分析。": "Full-tower S4R shell model with a door opening, supporting linear static and modal analyses.",
  "解析设置": "Parsing Settings",
  "AI 辅助解析": "AI-Assisted Parsing",
  "纯 AI 参数形成": "Full AI Parameter Formation",
  "本地优先 + AI 补全": "Local First + AI Completion",
  "仅本地解析": "Local Parsing Only",
  "顾问调用策略": "Agent Execution Strategy",
  "单次整批解析（默认）": "Single-batch parsing (default)",
  "大数据分块并发解析": "Concurrent chunked parsing",
  "常规工程资料只调用一次；仅在内容过多时启用分块并发。": "Standard engineering data are processed in one call; enable concurrent chunks only for large inputs.",
  "工程资料": "Engineering Data",
  "可选": "Optional",
  "拖拽工程资料到此处": "Drag engineering files here",
  "文字层 PDF": "text-based PDF",
  "参考模型": "Reference Model",
  "可上传参考 INP、选择已验证模型，或直接采用当前能力配置。": "Upload a reference INP, select a validated model, or use the current capability profile.",
  "已验证模型库": "Validated Model Library",
  "选择相近模型（可选）": "Select a similar model (optional)",
  "或临时参考 INP": "or use a temporary reference INP",
  "选择参考 INP": "Select Reference INP",
  "未识别单元": "Unrecognized elements",
  "来源：已验证模型": "Source: validated model",
  "未提供参考模型，将使用当前风塔能力配置候选。": "No reference model was provided; candidates from the current wind-tower capability profile will be used.",
  "FEM 规范已确认": "FEM specification confirmed",
  "请在右侧审查 FEM 参数候选。": "Review the FEM parameter candidates in the right panel.",
  "解析工程资料与参考模型": "Parse Engineering Data & Reference Model",
  "工程资料和参考模型均可选；仅填写明确建模目标也可开始，由工程师确认全部建议值。": "Engineering data and a reference model are optional. A clear modeling objective is sufficient to begin; all suggested values require engineer confirmation.",
  "解析状态": "Parsing Status",
  "解析": "Parsed",
  "冲突确认": "Conflict Resolution",
  "正式参数集已建立": "Official parameter set created",
  "高级操作": "Advanced Actions",
  "追加资料": "Add Files",
  "重新解析": "Reparse",
  "移除参考模型": "Remove Reference Model",
  "替换为所选参考 INP": "Replace with Selected Reference INP",
  "替换为所选模型规范": "Replace with Selected Model Specification",
  "技术详情": "Technical Details",
  "批次编号": "Batch ID",
  "解析模式": "Parsing Mode",
  "LLM 候选映射": "LLM Candidate Mapping",
  "确定性解析": "Deterministic Parsing",
  "调用策略": "Execution Strategy",
  "大数据分块并发": "Concurrent Large-Data Chunks",
  "单次整批": "Single Batch",
  "模型服务": "Model Service",
  "本地解析": "Local Parser",
  "工程知识库": "Engineering Knowledge Base",
  "FEM 知识库": "FEM Knowledge Base",
  "未索引": "Not Indexed",
  "项目": "Project",
  "待创建": "Not Created",
  "当前步骤": "Current Step",
  "工程状态": "Engineering Status",
  "参数修改": "Parameter Modification",
  "参数审查": "Parameter Review",
  "待建立": "Not Created",
  "自然语言修改": "Natural-Language Edit",
  "直接编辑参数": "Direct Parameter Edit",
  "证据": "Evidence",
  "版本": "Revisions",
  "工程参数": "Engineering Parameters",
  "FEM 参数": "FEM Parameters",
  "去确认参数": "Review Parameters",
  "修改目标": "Modification Objective",
  "例如：门洞宽度增加 5%，全局网格尺寸调整为 350 mm": "Example: increase the door width by 5% and set the global mesh size to 350 mm",
  "LLM 仅映射至允许参数，不生成节点、单元或 INP。": "The LLM maps only to permitted parameters; it does not generate nodes, elements, or INP files.",
  "差异预览": "Change Preview",
  "工程师确认并创建新版本": "Engineer Confirm & Create Revision",
  "先预览，再确认": "Preview before confirmation",
  "预览差异": "Preview Changes",
  "模型": "Model",
  "验证": "Validation",
  "结果": "Results",
  "文件": "Files",
  "关闭抽屉": "Close Drawer",
  "几何与门洞": "Geometry & Door Opening",
  "材料截面": "Material & Section",
  "接触与预紧": "Contact & Preload",
  "网格": "Mesh",
  "荷载": "Loads",
  "边界": "Boundary Conditions",
  "分析任务": "Analysis Tasks",
  "来自结构资料与工程师确认": "From structural data and engineer confirmation",
  "来自参考规范与能力配置": "From reference specifications and capability profiles",
  "塔段参数": "Tower Section Parameters",
  "段": "Section",
  "底标高": "Bottom Elevation",
  "顶标高": "Top Elevation",
  "底直径": "Bottom Diameter",
  "顶直径": "Top Diameter",
  "厚度": "Thickness",
  "启用": "Enabled",
  "禁用": "Disabled",
  "等待输入资料": "Awaiting Input Data",
  "上传工程资料后，两个顾问分别形成工程参数与 FEM 规范候选。": "After engineering files are uploaded, two agents independently generate engineering-parameter and FEM-specification candidates.",
  "L2 工程目标已接收": "L2 Engineering Objective Received",
  "当前局部法兰能力没有独立工程参数表；请在 FEM 参数页确认塔节、法兰、螺栓、接触、预紧和边界候选。": "The current local-flange capability has no separate engineering parameter table. Confirm the tower section, flange, bolt, contact, preload, and boundary candidates under FEM Parameters.",
  "工程参数尚未就绪": "Engineering Parameters Not Ready",
  "等待工程资料提取完成，缺失值将保留为空并由工程师补充。": "Waiting for engineering data extraction; missing values remain blank for completion by the engineer.",
  "工程参数候选": "Engineering Parameter Candidates",
  "工程顾问只映射带来源的工程事实；参考项目值和 LLM 建议不会自动写入。": "The engineering agent maps only sourced engineering facts; reference-project values and LLM suggestions are not applied automatically.",
  "FEM 规范尚未就绪": "FEM Specification Not Ready",
  "请选择模型库、上传参考 INP，或使用当前能力配置。": "Select a library model, upload a reference INP, or use the current capability profile.",
  "FEM 参数候选": "FEM Parameter Candidates",
  "FEM 顾问解释建模规范；网格拓扑、Abaqus 关键词和 INP 均由确定性生成器负责。": "The FEM agent interprets modeling specifications; mesh topology, Abaqus keywords, and INP files are produced by deterministic generators.",
  "固定规则": "Fixed Rule",
  "证据链：": "Evidence chain: ",
  "可复用规范": "Reusable Specifications",
  "需工程师确认": "Engineer Confirmation Required",
  "已确认": "Confirmed",
  "待确认": "Pending Confirmation",
  "参考项目值": "Reference-Project Value",
  "LLM 映射": "LLM Mapping",
  "LLM 建议": "LLM Suggestion",
  "LLM 推荐": "LLM Recommendation",
  "能力默认": "Capability Default",
  "能力配置": "Capability Profile",
  "工程师输入": "Engineer Input",
  "工程观测": "Engineering Observation",
  "规则派生": "Rule Derived",
  "源数据": "Source Data",
  "FEM 资料": "FEM Data",
  "已验证模型": "Validated Model",
  "确认全部参数并建立项目": "Confirm All Parameters & Create Project",
  "工程": "Engineering",
  "缺失": "Missing",
  "整体网格": "Global Mesh",
  "法兰": "Flange",
  "接触面": "Contact Surfaces",
  "螺栓": "Bolts",
  "门洞局部": "Door Region",
  "三维": "3D",
  "正视": "Front",
  "侧视": "Side",
  "俯视": "Top",
  "网格视角": "Mesh View",
  "Mises 应力": "von Mises Stress",
  "位移 U": "Displacement U",
  "变形 ×": "Deformation ×",
  "结果云图": "Result Contour",
  "网格预览": "Mesh Preview",
  "节点": "Nodes",
  "S4R 单元": "S4R Elements",
  "B31 螺栓": "B31 Bolts",
  "门洞单元": "Door-Region Elements",
  "等待网格质量检查": "Awaiting mesh quality check",
  "刷新预览": "Refresh Preview",
  "工程视角导航": "Engineering View Navigation",
  "正视图": "Front View",
  "侧视图": "Side View",
  "俯视图": "Top View",
  "ODB 结果": "ODB Results",
  "ODB 结果网格尚未生成": "ODB result mesh has not been generated",
  "确认参数后生成网格预览": "Confirm parameters to generate a mesh preview",
  "视图支持整体、门洞局部及四种工程视角": "The viewport supports global and door-region views in four engineering orientations",
  "未检测到门洞局部单元，已保持整体视图": "No door-region elements were detected; the global view is retained",
  "参数证据审查": "Parameter Evidence Review",
  "正在读取证据链…": "Loading evidence chain…",
  "返回参数列表": "Back to Parameter List",
  "未找到参数追溯信息。": "No parameter trace was found.",
  "未命名来源": "Unnamed Source",
  "位置未记录": "Location Not Recorded",
  "没有可定位来源，正式使用应被阻断。": "No traceable source is available; production use must be blocked.",
  "Validation 尚未运行": "Validation has not been run",
  "参数、模型/网格、求解/结果将分别显示。": "Parameters, model/mesh, and solver/results are reported separately.",
  "返回参数审查": "Back to Parameter Review",
  "等待项目": "Awaiting Project",
  "正式参数集建立后可执行模型门禁和求解。": "Model gates and solving become available after the official parameter set is created.",
  "生成并检查网格": "Generate & Check Mesh",
  "生成模型文件": "Generate Model Files",
  "执行 Datacheck": "Run Datacheck",
  "诊断与修复预览": "Diagnostics & Repair Preview",
  "可审查的参数补丁": "Reviewable Parameter Patch",
  "需要人工处理": "Manual Intervention Required",
  "发布为已验证模型版本": "Publish as Validated Model Revision",
  "显式求解任务": "Explicit Solver Tasks",
  "线弹性静力": "Linear Static",
  "模态分析": "Modal Analysis",
  "提交计算": "Submit Analysis",
  "求解任务": "Solver Tasks",
  "暂无结果": "No Results",
  "请先建立项目并显式提交 Abaqus。": "Create a project and explicitly submit the Abaqus analysis first.",
  "最大位移": "Maximum Displacement",
  "最大 Mises 应力": "Maximum von Mises Stress",
  "反力平衡": "Reaction-Force Balance",
  "一阶频率": "First Natural Frequency",
  "求解状态": "Solver Status",
  "刷新": "Refresh",
  "待生成": "Not Generated",
  "ODB 完整性": "ODB Integrity",
  "静力结果": "Static Results",
  "模态结果": "Modal Results",
  "在中间视区查看云图": "View Contour in Central Viewport",
  "暂无核心文件": "No Core Files",
  "参数、网格和模型构建后自动登记制品。": "Artifacts are registered automatically after parameter, mesh, and model generation.",
  "文件预览": "File Preview",
  "下载": "Download",
  "执行网格、构建或求解后刷新文件清单。": "Refresh the file list after meshing, building, or solving.",
  "仅显示正式参数、证据追踪、模型文件、检查报告和计算结果；运行所需的内部文件已隐藏。": "Only official parameters, evidence traces, model files, check reports, and analysis results are shown; internal runtime files are hidden.",
  "预览": "Preview",
  "适用于当前能力": "Compatible with Current Capability",
  "部分信息可参考": "Partially Applicable",
  "不适用于当前能力": "Incompatible with Current Capability",
  "工程师复核": "Engineer Reviewed",
  "模型检查通过": "Model Check Passed",
  "求解验证通过": "Solver Verified",
  "启用门洞": "Enable Door Opening",
  "门洞宽度": "Door Width",
  "门洞高度": "Door Height",
  "门洞底标高": "Door Bottom Elevation",
  "门洞环向角度": "Door Circumferential Angle",
  "材料名称": "Material Name",
  "弹性模量": "Elastic Modulus",
  "泊松比": "Poisson's Ratio",
  "材料密度": "Material Density",
  "屈服强度": "Yield Strength",
  "壳单元类型": "Shell Element Type",
  "全局网格尺寸": "Global Mesh Size",
  "门洞局部网格尺寸": "Door-Region Mesh Size",
  "门洞环向细化边界": "Door Circumferential Refinement Margin",
  "门洞高度向细化边界": "Door Vertical Refinement Margin",
  "门洞环向层数": "Door Circumferential Layers",
  "顶部等效质量": "Equivalent Top Mass",
  "顶部水平力": "Top Horizontal Force",
  "顶部力方向": "Top-Force Direction",
  "风压": "Wind Pressure",
  "静力分析": "Static Analysis",
  "模态阶数": "Number of Modes",
  "底部边界条件": "Base Boundary Condition",
  "通过": "Passed",
  "就绪": "Ready",
  "完成（有警告）": "Completed with Warnings",
  "解析中": "Parsing",
  "排队中": "Queued",
  "运行中": "Running",
  "已提交": "Submitted",
  "失败": "Failed",
  "警告": "Warning",
  "部分完成": "Partially Completed",
  "适用": "Compatible",
  "不适用": "Incompatible",
  "需要补充": "Input Required",
  "需要确认": "Confirmation Required",
  "提取中": "Extracting",
  "映射中": "Mapping",
  "其他": "Other",
  "参数": "Parameters",
  "静力 INP": "Static INP",
  "模态 INP": "Modal INP",
  "Datacheck 与日志": "Datacheck & Logs",
};

const EXACT_ENTRIES = Object.entries(ENGLISH).sort(([left], [right]) => right.length - left.length);

function translateText(source: string) {
  if (!/[\u3400-\u9fff]/u.test(source)) return source;
  const leading = source.match(/^\s*/u)?.[0] ?? "";
  const trailing = source.match(/\s*$/u)?.[0] ?? "";
  const core = source.slice(leading.length, source.length - trailing.length);
  if (ENGLISH[core]) return `${leading}${ENGLISH[core]}${trailing}`;

  let translated = core
    .replace(/^建立带门洞风电塔筒 S4R 壳模型，执行线弹性静力与模态分析。$/u, "Create an S4R shell model of a wind-turbine tower with a door opening, and perform linear static and modal analyses.")
    .replace(/^建立局部上下塔节单法兰模型，采用 S4R 塔壁与法兰、B31 螺栓、法兰面接触及原生预紧，执行预紧—锁定—外载三个非线性静力步。$/u, "Create a local single-flange model of two tower sections using S4R tower walls and flanges, B31 bolts, flange-surface contact, and native preload; perform preload, lock, and external-load nonlinear static steps.")
    .replace(/^另有 (\d+) 个文件$/u, "$1 additional files")
    .replace(/^另有 (\d+) 个来源候选可追溯$/u, "$1 additional source candidates are traceable")
    .replace(/^还需补充 (\d+) 项$/u, "$1 items still required")
    .replace(/^还有 (\d+) 项参数待确认$/u, "$1 parameters pending confirmation")
    .replace(/^(\d+) 个文件 · 项目 /u, "$1 files · Project ")
    .replace(/^(\d+) 个单元$/u, "$1 elements")
    .replace(/^(\d+) 个分析步$/u, "$1 analysis steps")
    .replace(/^(\d+) 个候选$/u, "$1 candidates")
    .replace(/^(\d+) 项网格警告$/u, "$1 mesh warnings")
    .replace(/^(\d+) 项变化$/u, "$1 changes")
    .replace(/^(\d+) 项待修改$/u, "$1 pending changes")
    .replace(/^(\d+) 项$/u, "$1 items")
    .replace(/^(\d+) 段$/u, "$1 sections")
    .replace(/^第 (\d+) 段/u, "Section $1 ")
    .replace(/^匹配 (\d+)%$/u, "$1% match")
    .replace(/^当前不处理（(\d+)）$/u, "Deferred ($1)");

  for (const [zh, en] of EXACT_ENTRIES) translated = translated.split(zh).join(en);
  return `${leading}${translated}${trailing}`;
}

type LocalizationContextValue = {
  language: Language;
  setLanguage: (language: Language) => void;
};

const LocalizationContext = createContext<LocalizationContextValue>({
  language: "zh-CN",
  setLanguage: () => undefined,
});

const originalText = new WeakMap<Text, string>();
const appliedText = new WeakMap<Text, string>();
const originalAttributes = new WeakMap<Element, Map<string, string>>();
const appliedAttributes = new WeakMap<Element, Map<string, string>>();
const LOCALIZED_ATTRIBUTES = ["aria-label", "placeholder", "title"] as const;

function localizeTextNode(node: Text, language: Language) {
  const current = node.nodeValue ?? "";
  if (current !== appliedText.get(node)) originalText.set(node, current);
  const source = originalText.get(node) ?? current;
  const next = language === "en-US" ? translateText(source) : source;
  appliedText.set(node, next);
  if (current !== next) node.nodeValue = next;
}

function localizeElement(element: Element, language: Language) {
  const originals = originalAttributes.get(element) ?? new Map<string, string>();
  const applied = appliedAttributes.get(element) ?? new Map<string, string>();
  for (const attribute of LOCALIZED_ATTRIBUTES) {
    const current = element.getAttribute(attribute);
    if (current == null) continue;
    if (current !== applied.get(attribute)) originals.set(attribute, current);
    const source = originals.get(attribute) ?? current;
    const next = language === "en-US" ? translateText(source) : source;
    applied.set(attribute, next);
    if (current !== next) element.setAttribute(attribute, next);
  }
  originalAttributes.set(element, originals);
  appliedAttributes.set(element, applied);
}

function localizeTree(root: Node, language: Language) {
  if (root.nodeType === Node.TEXT_NODE) localizeTextNode(root as Text, language);
  if (root.nodeType === Node.ELEMENT_NODE) localizeElement(root as Element, language);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    if (node.nodeType === Node.TEXT_NODE) localizeTextNode(node as Text, language);
    else localizeElement(node as Element, language);
    node = walker.nextNode();
  }
}

export function LocalizationProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(() => (
    window.localStorage.getItem(STORAGE_KEY) === "en-US" ? "en-US" : "zh-CN"
  ));

  const value = useMemo(() => ({
    language,
    setLanguage(next: Language) {
      window.localStorage.setItem(STORAGE_KEY, next);
      setLanguageState(next);
    },
  }), [language]);

  useEffect(() => {
    document.documentElement.lang = language;
    document.documentElement.dataset.language = language;
    document.title = language === "en-US"
      ? "Wind Tower CAE Modeling Workbench"
      : "风电塔筒 CAE 建模工作台";
    const root = document.getElementById("root") ?? document.body;
    localizeTree(root, language);
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "characterData") localizeTextNode(mutation.target as Text, language);
        mutation.addedNodes.forEach((node) => localizeTree(node, language));
        if (mutation.type === "attributes") localizeElement(mutation.target as Element, language);
      }
    });
    observer.observe(root, { attributes: true, characterData: true, childList: true, subtree: true });
    return () => observer.disconnect();
  }, [language]);

  return <LocalizationContext.Provider value={value}>{children}</LocalizationContext.Provider>;
}

export function useLocalization() {
  return useContext(LocalizationContext);
}

export function LanguageSwitcher() {
  const { language, setLanguage } = useLocalization();
  return (
    <div className="language-switcher" aria-label={language === "zh-CN" ? "语言切换" : "Language selector"}>
      <Languages aria-hidden="true" size={15} />
      <button
        aria-pressed={language === "zh-CN"}
        className={language === "zh-CN" ? "active" : ""}
        onClick={() => setLanguage("zh-CN")}
        type="button"
      >中文</button>
      <span aria-hidden="true" />
      <button
        aria-pressed={language === "en-US"}
        className={language === "en-US" ? "active" : ""}
        onClick={() => setLanguage("en-US")}
        type="button"
      >EN</button>
    </div>
  );
}
