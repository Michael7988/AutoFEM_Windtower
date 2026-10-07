from __future__ import annotations

import argparse
import csv
import html
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


FORMATS = (
    "xlsx", "csv", "json", "yaml", "txt", "docx", "pptx",
    "text_pdf", "scan_pdf", "png",
)
FORMAT_LABELS = {
    "xlsx": "XLSX",
    "csv": "CSV",
    "json": "JSON",
    "yaml": "YAML",
    "txt": "TXT",
    "docx": "DOCX",
    "pptx": "PPTX",
    "text_pdf": "文字PDF",
    "scan_pdf": "扫描PDF",
    "png": "分页PNG",
}
PALETTE = {
    "navy": "#16415D",
    "blue": "#3B7EA1",
    "cyan": "#65AFC1",
    "green": "#2F8F83",
    "orange": "#D98545",
    "red": "#B85555",
    "gray": "#87949D",
    "light": "#E8F0F3",
    "ink": "#1E2D35",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the paper-level G3-E HTML report and publication figures."
    )
    parser.add_argument("--dataset-root", type=Path, required=True)
    args = parser.parse_args()
    report = generate_report(args.dataset_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def generate_report(dataset_root: str | Path) -> dict[str, Any]:
    root = Path(dataset_root).resolve()
    manifest = _read_json(root / "manifest.json")
    deterministic = _read_json(root / "results" / "deterministic_summary.json")
    _validate_dataset(manifest, deterministic)
    llm_path = root / "results" / "llm_summary.json"
    llm = _read_json(llm_path) if llm_path.is_file() else None
    llm_complete = bool(llm and llm.get("run_count") == 120)

    output = root / "paper_report_assets"
    figures = output / "figures"
    source_data = output / "source_data"
    figures.mkdir(parents=True, exist_ok=True)
    source_data.mkdir(parents=True, exist_ok=True)
    _configure_matplotlib()

    case_rows = _case_rows(root, manifest)
    format_rows = _format_rows(deterministic, llm if llm_complete else None)
    case_format_rows = _case_format_rows(deterministic, llm if llm_complete else None)
    failure_rows = _failure_rows(deterministic, llm if llm_complete else None)
    _write_csv(source_data / "figure1_experiment_design.csv", _design_rows(manifest))
    _write_csv(source_data / "figure2_format_performance.csv", format_rows)
    _write_csv(source_data / "figure3_efficiency_cost.csv", format_rows)
    _write_csv(source_data / "figure4_case_format_matrix.csv", case_format_rows)
    _write_csv(source_data / "figure5_failure_profile.csv", failure_rows)
    _write_csv(source_data / "table1_case_models.csv", case_rows)

    figure_paths = {
        "fig1": _figure1(figures, manifest),
        "fig2": _figure2(figures, format_rows, llm_complete),
        "fig3": _figure3(figures, format_rows, llm_complete),
        "fig4": _figure4(figures, case_format_rows, llm_complete),
        "fig5": _figure5(figures, failure_rows, llm_complete),
    }
    html_path = root / "G3E_跨格式LLM解析等价性_论文级实验报告.html"
    html_path.write_text(
        _build_html(
            root,
            manifest,
            deterministic,
            llm if llm_complete else None,
            case_rows,
            format_rows,
            failure_rows,
            figure_paths,
        ),
        encoding="utf-8",
    )
    return {
        "report": str(html_path),
        "figures": len(figure_paths),
        "source_data_files": len(list(source_data.glob("*.csv"))),
        "deterministic_runs": deterministic["run_count"],
        "llm_runs_included": llm["run_count"] if llm_complete else 0,
        "llm_status": "included" if llm_complete else "not_available",
    }


def _configure_matplotlib() -> None:
    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [
            "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS",
            "DejaVu Sans",
        ],
        "axes.unicode_minus": False,
        "svg.fonttype": "none",
        "pdf.fonttype": 42,
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "axes.spines.right": False,
        "axes.spines.top": False,
        "axes.linewidth": 0.7,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.frameon": False,
    })


def _save_figure(fig: plt.Figure, directory: Path, name: str) -> dict[str, Path]:
    paths = {
        "svg": directory / f"{name}.svg",
        "pdf": directory / f"{name}.pdf",
        "tiff": directory / f"{name}.tiff",
    }
    fig.savefig(paths["svg"], bbox_inches="tight")
    fig.savefig(paths["pdf"], bbox_inches="tight")
    fig.savefig(paths["tiff"], dpi=600, bbox_inches="tight")
    plt.close(fig)
    return paths


def _figure1(directory: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    fig, ax = plt.subplots(figsize=(7.1, 3.25))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 5)
    ax.axis("off")
    boxes = [
        (0.25, 2.0, 1.7, 1.25, "4个匿名案例\n195个塔段", PALETTE["navy"]),
        (2.35, 2.0, 1.7, 1.25, "10种输入载体\n40个格式条件", PALETTE["blue"]),
        (4.45, 2.0, 1.7, 1.25, "本地确定性提取\n文字层优先", PALETTE["cyan"]),
        (6.55, 2.0, 1.7, 1.25, "受限LLM映射\n仅补充未决参数", PALETTE["green"]),
        (8.65, 2.0, 1.1, 1.25, "参数门禁\n模型编译", PALETTE["orange"]),
    ]
    for x, y, w, h, text, color in boxes:
        patch = FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.04,rounding_size=0.08",
            facecolor=color, edgecolor="none",
        )
        ax.add_patch(patch)
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color="white", weight="bold")
    for index in range(len(boxes) - 1):
        left = boxes[index]
        right = boxes[index + 1]
        ax.add_patch(FancyArrowPatch(
            (left[0] + left[2] + 0.05, 2.63), (right[0] - 0.05, 2.63),
            arrowstyle="-|>", mutation_scale=12, linewidth=1.2, color=PALETTE["gray"],
        ))
    ax.text(5, 4.35, "G3-E跨格式解析等价性实验设计", ha="center", fontsize=12, weight="bold", color=PALETTE["ink"])
    ax.text(5, 3.82, "每种载体独立成批；不同格式不得相互补充；工程师不得依据隐藏真值改值", ha="center", color=PALETTE["gray"])
    ax.text(2.0, 0.75, "确定性基线：4×10 = 40次", ha="center", color=PALETTE["navy"], weight="bold")
    ax.text(5.0, 0.75, "LLM模式：4×10×3 = 120次", ha="center", color=PALETTE["green"], weight="bold")
    ax.text(8.0, 0.75, "输出：参数、模型、效率与失败模式", ha="center", color=PALETTE["orange"], weight="bold")
    return _save_figure(fig, directory, "Fig1_experiment_design")


def _figure2(directory: Path, rows: list[dict[str, Any]], llm_complete: bool) -> dict[str, Path]:
    selected = [row for row in rows if row["mode"] == ("llm" if llm_complete else "deterministic")]
    labels = [FORMAT_LABELS[row["carrier"]] for row in selected]
    y = np.arange(len(labels))
    fig, axes = plt.subplots(1, 3, figsize=(7.1, 3.8), sharey=True)
    metrics = [
        ("mean_recall", "参数Recall", PALETTE["blue"]),
        ("parameter_equivalence_rate", "参数集完整等价率", PALETTE["green"]),
        ("model_equivalence_rate", "模型等价率", PALETTE["orange"]),
    ]
    for ax, (key, title, color) in zip(axes, metrics):
        values = [row[key] * 100 for row in selected]
        ax.barh(y, values, color=color, alpha=0.88, height=0.66)
        ax.set_xlim(0, 104)
        ax.set_title(title, weight="bold")
        ax.set_xlabel("%")
        ax.grid(axis="x", color="#DCE5E9", linewidth=0.6)
        for index, value in enumerate(values):
            ax.text(min(value + 2, 99), index, f"{value:.1f}", va="center", fontsize=6.5)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(labels)
    axes[0].invert_yaxis()
    mode = "LLM辅助流程" if llm_complete else "确定性提取基线"
    fig.suptitle(f"不同输入载体的参数与模型恢复性能（{mode}）", fontsize=11, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return _save_figure(fig, directory, "Fig2_format_performance")


def _figure3(directory: Path, rows: list[dict[str, Any]], llm_complete: bool) -> dict[str, Path]:
    selected = [row for row in rows if row["mode"] == ("llm" if llm_complete else "deterministic")]
    labels = [FORMAT_LABELS[row["carrier"]] for row in selected]
    x = np.arange(len(labels))
    fig, axes = plt.subplots(1, 3, figsize=(7.1, 3.25))
    axes[0].bar(x, [row["mean_latency_ms"] / 1000 for row in selected], color=PALETTE["blue"])
    axes[0].set_ylabel("s")
    axes[0].set_title("平均处理延迟", weight="bold")
    token_values = [row["mean_total_tokens"] for row in selected]
    axes[1].bar(x, token_values, color=PALETTE["green"])
    axes[1].set_ylabel("tokens/run")
    axes[1].set_title("平均Token消耗", weight="bold")
    if max(token_values, default=0) == 0:
        axes[1].set_ylim(0, 1)
        axes[1].text(
            0.5, 0.52, "0\n确定性基线未调用云端LLM",
            transform=axes[1].transAxes, ha="center", va="center",
            color=PALETTE["green"], weight="bold",
        )
    axes[2].bar(x, [row["mean_ocr_pages"] for row in selected], color=PALETTE["orange"])
    axes[2].set_ylabel("pages/run")
    axes[2].set_title("OCR页面需求", weight="bold")
    for ax in axes:
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=55, ha="right")
        ax.grid(axis="y", color="#DCE5E9", linewidth=0.6)
    fig.suptitle("跨格式解析的时间与计算成本", fontsize=11, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return _save_figure(fig, directory, "Fig3_efficiency_cost")


def _figure4(directory: Path, rows: list[dict[str, Any]], llm_complete: bool) -> dict[str, Path]:
    mode = "llm" if llm_complete else "deterministic"
    selected = [row for row in rows if row["mode"] == mode]
    cases = sorted({row["case_id"] for row in selected})
    matrix = np.full((len(cases), len(FORMATS)), np.nan)
    for row in selected:
        matrix[cases.index(row["case_id"]), FORMATS.index(row["carrier"])] = row["model_equivalence_rate"]
    fig, ax = plt.subplots(figsize=(7.1, 2.8))
    image = ax.imshow(matrix, vmin=0, vmax=1, cmap=mpl.colors.LinearSegmentedColormap.from_list(
        "equivalence", ["#EEF2F3", PALETTE["cyan"], PALETTE["green"]]
    ), aspect="auto")
    ax.set_xticks(np.arange(len(FORMATS)))
    ax.set_xticklabels([FORMAT_LABELS[item] for item in FORMATS], rotation=45, ha="right")
    ax.set_yticks(np.arange(len(cases)))
    ax.set_yticklabels(cases)
    for i in range(len(cases)):
        for j in range(len(FORMATS)):
            value = matrix[i, j]
            ax.text(j, i, "—" if math.isnan(value) else f"{value:.2f}", ha="center", va="center", fontsize=6.5, color=PALETTE["ink"])
    colorbar = fig.colorbar(image, ax=ax, fraction=0.025, pad=0.02)
    colorbar.set_label("模型等价率")
    ax.set_title("案例×格式模型等价性矩阵", fontsize=11, weight="bold")
    fig.tight_layout()
    return _save_figure(fig, directory, "Fig4_case_format_matrix")


def _figure5(directory: Path, rows: list[dict[str, Any]], llm_complete: bool) -> dict[str, Path]:
    mode = "llm" if llm_complete else "deterministic"
    selected = [row for row in rows if row["mode"] == mode]
    labels = [FORMAT_LABELS[item] for item in FORMATS]
    failures = sorted({item for row in selected for item in row["failure_counts"]})
    colors = [PALETTE["green"], PALETTE["orange"], PALETTE["red"], PALETTE["gray"], PALETTE["blue"]]
    fig, ax = plt.subplots(figsize=(7.1, 3.3))
    bottom = np.zeros(len(FORMATS))
    for index, failure in enumerate(failures):
        values = np.array([
            next(row for row in selected if row["carrier"] == carrier)["failure_counts"].get(failure, 0)
            for carrier in FORMATS
        ])
        ax.bar(labels, values, bottom=bottom, label=_failure_label(failure), color=colors[index % len(colors)])
        bottom += values
    ax.set_ylabel("运行数")
    ax.set_title("不同载体的运行状态与失败分类", fontsize=11, weight="bold")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(ncol=min(4, len(failures)), loc="upper center", bbox_to_anchor=(0.5, -0.22))
    ax.grid(axis="y", color="#DCE5E9", linewidth=0.6)
    fig.tight_layout()
    return _save_figure(fig, directory, "Fig5_failure_profile")


def _build_html(
    root: Path,
    manifest: dict[str, Any],
    deterministic: dict[str, Any],
    llm: dict[str, Any] | None,
    case_rows: list[dict[str, Any]],
    format_rows: list[dict[str, Any]],
    failure_rows: list[dict[str, Any]],
    figures: dict[str, dict[str, Path]],
) -> str:
    llm_complete = llm is not None
    baseline_rows = [row for row in format_rows if row["mode"] == "deterministic"]
    xlsx = next(row for row in baseline_rows if row["carrier"] == "xlsx")
    json_row = next(row for row in baseline_rows if row["carrier"] == "json")
    figure_svg = {key: paths["svg"].read_text(encoding="utf-8") for key, paths in figures.items()}
    llm_status = (
        "完整120次LLM模式结果已纳入统计。"
        if llm_complete
        else "LLM阶段未形成有效完整数据集；外部服务DNS不可达产生的中断请求已排除，不作为算法性能结果。"
    )
    direct_result = _manuscript_result_text(baseline_rows, llm)
    table1 = _html_table(case_rows, [
        ("case_id", "案例"), ("tower_segment_count", "塔段数"),
        ("node_count", "节点数"), ("element_count", "S4R单元数"),
        ("analysis_tasks", "分析任务"),
    ])
    table2 = _html_table(baseline_rows, [
        ("format_label", "格式"), ("run_count", "n"), ("mean_precision_pct", "Precision/%"),
        ("mean_recall_pct", "Recall/%"), ("parameter_equivalence_pct", "参数集等价/%"),
        ("model_equivalence_pct", "模型等价/%"), ("mean_latency_s", "延迟/s"),
        ("mean_ocr_pages", "OCR页/次"),
    ])
    table3_rows = _mode_comparison_rows(deterministic, llm)
    table3 = _html_table(table3_rows, [
        ("mode_label", "流程"), ("runs", "运行数"), ("valid_runs", "有效运行"),
        ("cloud_calls", "云端调用"), ("tokens", "Token"), ("conclusion", "可支持结论"),
    ])
    table4 = _html_table(_failure_interpretation_rows(), [
        ("condition", "现象"), ("mechanism", "原因"), ("impact", "工程影响"), ("treatment", "统计处理"),
    ])
    css = _report_css()
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>G3-E跨格式工程输入解析等价性实验报告</title><style>{css}</style></head>
<body><main>
<header><div class="eyebrow">G3-E · PAPER-READY EXPERIMENT REPORT</div>
<h1>跨格式工程输入解析与有限元模型等价性实验</h1>
<p class="subtitle">四组真实风电塔筒工程案例的确定性提取基线与受限LLM实验状态</p>
<div class="meta"><span>案例：4</span><span>载体：10</span><span>确定性运行：40</span><span>LLM有效运行：{llm["run_count"] if llm_complete else 0}</span></div></header>
<section class="status {'ok' if llm_complete else 'warn'}"><strong>数据状态：</strong>{llm_status}</section>
<nav><a href="#summary">摘要</a><a href="#methods">方法</a><a href="#results">结果</a><a href="#discussion">讨论</a><a href="#papertext">论文插入文本</a></nav>

<section id="summary"><h2>摘要式总结</h2>
<div class="key-grid"><div><b>40</b><span>确定性基线运行</span></div><div><b>{xlsx['model_equivalence_pct']:.0f}%</b><span>XLSX模型等价率</span></div><div><b>{json_row['mean_recall_pct']:.1f}%</b><span>JSON参数Recall</span></div><div><b>0</b><span>纳入统计的LLM失败样本</span></div></div>
<p>本实验将4组匿名化风电塔筒工程参数分别转换为10种输入载体，并在不允许跨格式互补信息的条件下评价参数恢复和确定性S4R模型重建能力。现有有效结果首先刻画本地确定性提取基线：规范化XLSX在4个案例中均通过参数门禁，并在补齐公开编译默认值后获得100%的模型等价率；JSON保留了较高的层级信息，平均参数Recall为{json_row['mean_recall_pct']:.1f}%，但少量必需字段缺失仍会触发门禁阻断。CSV、TXT、DOCX、PPTX和文字PDF在当前规则映射下只能恢复有限标量；扫描PDF与分页PNG被正确识别为需要OCR的输入。外部LLM服务因DNS不可达未形成可解释的完整结果，因此本报告不对不同格式的LLM性能作推断。</p>
</section>

<section id="methods"><h2>1 实验设计与评价方法</h2>
<p>四个案例均以正式参数集作为隔离真值，活动参数覆盖塔段、门洞、材料、S4R网格、边界荷载、静力与模态任务及输出需求。每种载体独立建立输入批次；文件名、项目ID、revision和时间戳不参与参数集比较。参数门禁通过后，确定性适配器重新生成静力与模态INP，并使用节点数、S4R单元数及规范化INP签名评价模型等价性。</p>
<div class="figure">{figure_svg['fig1']}<p class="caption"><b>图1｜G3-E跨格式解析等价性实验设计。</b> 输入首先进行本地确定性提取，只有未决目标参数才允许发送至受限LLM；LLM不生成节点、单元、Abaqus关键词或INP。</p></div>
<div class="formula"><b>参数Precision</b> = TP/(TP+FP)； <b>参数Recall</b> = TP/(TP+FN)； <b>完整等价</b>要求全部活动参数在字段容差内一致。</div>
<h3>表1｜匿名工程案例与参考有限元模型规模</h3>{table1}
</section>

<section id="results"><h2>2 实验结果</h2>
<h3>2.1 参数与模型恢复性能</h3>
<div class="figure">{figure_svg['fig2']}<p class="caption"><b>图2｜不同输入载体的参数与模型恢复性能。</b> 当前图展示确定性提取基线。XLSX的严格参数集等价率为0，是因为若干公开编译默认值未作为文件观测值返回；这些默认值补齐后，4个案例的节点、S4R单元及静力/模态INP签名均与参考模型一致。</p></div>
<h3>表2｜确定性提取基线的格式级汇总</h3>{table2}
<p>规范化XLSX的平均参数Precision为100%，Recall为{xlsx['mean_recall_pct']:.1f}%，说明已返回参数均与真值一致，未显式返回项主要是编译器公开默认值。JSON的Recall达到{json_row['mean_recall_pct']:.1f}%，显示层级键路径能够较完整地保留工程语义；然而参数门禁采用“必需字段全部满足”的保守策略，因此少量缺失仍阻断模型编译。来源定位率只反映已恢复候选是否具有证据引用，不等同于参数完整性。</p>

<h3>2.2 时间与OCR需求</h3>
<div class="figure">{figure_svg['fig3']}<p class="caption"><b>图3｜跨格式解析的时间与计算成本。</b> 确定性基线不产生云端Token。扫描PDF和PNG各有28个页面/图像被标识为需要OCR，该数量表示识别需求而非成功OCR结果。</p></div>

<h3>2.3 案例一致性与失败模式</h3>
<div class="figure">{figure_svg['fig4']}<p class="caption"><b>图4｜案例×格式模型等价性矩阵。</b> 每个单元格为对应案例和载体的模型等价率；确定性基线每个条件仅运行一次。</p></div>
<div class="figure">{figure_svg['fig5']}<p class="caption"><b>图5｜不同载体的运行状态与失败分类。</b> “blocked”表示证据提取已完成但参数门禁阻止编译；“failed”主要用于无文字层输入且OCR未启用的情况，不应解释为LLM推理失败。</p></div>
<h3>表3｜确定性基线与LLM辅助流程的数据状态</h3>{table3}
<h3>表4｜典型失败模式及工程解释</h3>{table4}
</section>

<section id="discussion"><h2>3 讨论</h2>
<p><b>结构化层级优于纯文本边界。</b> JSON以键路径保留了数组和嵌套关系，因此即使未通过最终门禁，其参数Recall仍显著高于CSV和自由文本载体。YAML理论上具有相同层级表达能力，但当前本地规则对其嵌套结构消费不足，说明“信息存在”与“生产解析器能够提升为正式参数”是两个不同问题。</p>
<p><b>门禁应与平均准确率分开报告。</b> 某些格式可以恢复大部分参数，但只要塔段数组、材料密度、荷载方向或边界条件缺失，模型便不会编译。这种保守行为降低自动通过率，却避免根据不完整证据生成看似有效的有限元模型。</p>
<p><b>图像载体需要单独评价OCR链路。</b> 扫描PDF和分页PNG没有文字层，本地提取只能记录页面位置并标记OCR需求。后续LLM/OCR实验应将文字识别错误、参数映射错误和门禁阻断分别统计，不能把三者合并为单一“LLM失败率”。</p>
<p><b>局限性。</b> 仅包含4个同结构族案例，且当前有效结果只覆盖确定性基线；不能据此推广到其他结构族，也不能对LLM模型之间的性能差异作推断。完整LLM结果需在网络恢复后按冻结配置重新执行120次模式运行。</p>
</section>

<section id="papertext"><h2>4 可直接插入论文的结果小节</h2>
<div class="paper-text"><h3>跨格式输入的确定性提取基线</h3><p>{html.escape(direct_result)}</p></div>
<h3>建议用于论文的精炼结论</h3>
<ol><li>规范化XLSX可由确定性解析器恢复足以重建等价S4R模型的参数。</li><li>JSON保留了较高的参数完整性，但形式完整不自动等于满足正式参数门禁。</li><li>文档与图像载体需要受限LLM/OCR补充；在获得完整有效运行前，不报告其LLM等价率。</li><li>参数门禁成功率、参数Recall和模型等价率应作为互补指标共同报告。</li></ol>
</section>

<section><h2>5 数据完整性与复现说明</h2>
<ul><li>数据集版本：{html.escape(str(manifest['schema_version']))}</li><li>有效确定性记录：{deterministic['run_count']}；完整LLM记录：{llm['run_count'] if llm_complete else 0}</li><li>所有图对应的source-data CSV与SVG、PDF、600 dpi TIFF已随报告生成。</li><li>外部服务连接失败的中断运行不进入性能统计。</li><li>报告不包含工程原文件名、绝对路径、密钥或内部批次ID。</li></ul>
</section>
<footer>G3-E paper-ready report · generated from frozen local experiment artifacts</footer>
</main></body></html>"""


def _case_rows(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for case in manifest["cases"]:
        signature = _read_json(root / case["ground_truth"]["reference_model_signature"])
        compile_data = signature["compile"]
        rows.append({
            "case_id": case["case_id"],
            "tower_segment_count": case["tower_segment_count"],
            "node_count": compile_data["node_count"],
            "element_count": compile_data["element_count"],
            "analysis_tasks": "静力、模态",
        })
    return rows


def _format_rows(deterministic: dict[str, Any], llm: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for summary in [deterministic, *([llm] if llm else [])]:
        mode = summary["mode"]
        for carrier in FORMATS:
            selected = [row for row in summary["runs"] if row["carrier"] == carrier]
            metrics = summary["formats"][carrier]
            row = {
                "mode": mode,
                "carrier": carrier,
                "format_label": FORMAT_LABELS[carrier],
                "run_count": len(selected),
                "mean_precision": metrics["mean_precision"],
                "mean_recall": metrics["mean_recall"],
                "parameter_equivalence_rate": metrics["parameter_equivalence"]["rate"] or 0.0,
                "parameter_ci_low": (metrics["parameter_equivalence"].get("ci95") or [0, 0])[0],
                "parameter_ci_high": (metrics["parameter_equivalence"].get("ci95") or [0, 0])[1],
                "model_equivalence_rate": metrics["model_equivalence"]["rate"] or 0.0,
                "model_ci_low": (metrics["model_equivalence"].get("ci95") or [0, 0])[0],
                "model_ci_high": (metrics["model_equivalence"].get("ci95") or [0, 0])[1],
                "mean_source_location_rate": metrics["mean_source_location_rate"],
                "mean_latency_ms": mean([item["latency_ms"] for item in selected]),
                "median_latency_ms": median([item["latency_ms"] for item in selected]),
                "sd_latency_ms": pstdev([item["latency_ms"] for item in selected]) if len(selected) > 1 else 0.0,
                "mean_total_tokens": mean([item["total_tokens"] for item in selected]),
                "mean_ocr_pages": mean([item["ocr_pages"] for item in selected]),
                "confirmation_rate": mean([float(bool(item["requires_confirmation"])) for item in selected]),
                "conflict_rate": mean([float(item["conflict_count"] > 0) for item in selected]),
            }
            row.update({
                "mean_precision_pct": round(row["mean_precision"] * 100, 2),
                "mean_recall_pct": round(row["mean_recall"] * 100, 2),
                "parameter_equivalence_pct": round(row["parameter_equivalence_rate"] * 100, 2),
                "model_equivalence_pct": round(row["model_equivalence_rate"] * 100, 2),
                "mean_latency_s": round(row["mean_latency_ms"] / 1000, 3),
            })
            rows.append(row)
    return rows


def _case_format_rows(deterministic: dict[str, Any], llm: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for summary in [deterministic, *([llm] if llm else [])]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for run in summary["runs"]:
            groups.setdefault((run["case_id"], run["carrier"]), []).append(run)
        for (case_id, carrier), selected in sorted(groups.items()):
            rows.append({
                "mode": summary["mode"],
                "case_id": case_id,
                "carrier": carrier,
                "repetitions": len(selected),
                "parameter_equivalence_rate": mean([float(item["parameter_equivalent"]) for item in selected]),
                "model_equivalence_rate": mean([float(item["model_signature_equivalent"]) for item in selected]),
                "parameter_hash_consistency": len({item.get("parameter_hash") for item in selected}) <= 1,
            })
    return rows


def _failure_rows(deterministic: dict[str, Any], llm: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for summary in [deterministic, *([llm] if llm else [])]:
        for carrier in FORMATS:
            selected = [row for row in summary["runs"] if row["carrier"] == carrier]
            counts = Counter(row["failure_class"] for row in selected)
            rows.append({
                "mode": summary["mode"],
                "carrier": carrier,
                "failure_counts": dict(counts),
                **{f"failure_{key}": value for key, value in sorted(counts.items())},
            })
    return rows


def _design_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "case_id": case["case_id"],
        "carrier": carrier["carrier"],
        "condition_id": carrier["condition_id"],
        "tower_segment_count": case["tower_segment_count"],
    } for case in manifest["cases"] for carrier in case["carriers"]]


def _mode_comparison_rows(deterministic: dict[str, Any], llm: dict[str, Any] | None) -> list[dict[str, Any]]:
    deterministic_valid = sum(row["batch_status"] == "ready" for row in deterministic["runs"])
    rows = [{
        "mode_label": "确定性提取基线",
        "runs": deterministic["run_count"],
        "valid_runs": deterministic_valid,
        "cloud_calls": 0,
        "tokens": 0,
        "conclusion": "可用于评价本地提取和门禁行为",
    }]
    if llm:
        rows.append({
            "mode_label": "受限LLM辅助",
            "runs": llm["run_count"],
            "valid_runs": sum(row["batch_status"] == "ready" for row in llm["runs"]),
            "cloud_calls": sum(int(row.get("llm_calls") or 0) for row in llm["runs"]),
            "tokens": sum(int(row.get("total_tokens") or 0) for row in llm["runs"]),
            "conclusion": "可用于评价跨格式LLM/OCR等价性",
        })
    else:
        rows.append({
            "mode_label": "受限LLM辅助",
            "runs": 0,
            "valid_runs": 0,
            "cloud_calls": 0,
            "tokens": 0,
            "conclusion": "外部服务不可达，未纳入性能结论",
        })
    return rows


def _failure_interpretation_rows() -> list[dict[str, str]]:
    return [
        {"condition": "ready", "mechanism": "参数门禁通过", "impact": "允许确定性编译", "treatment": "计入自动可用"},
        {"condition": "blocked", "mechanism": "缺少至少一个必需参数", "impact": "禁止生成模型", "treatment": "计入提取结果，不计入模型成功"},
        {"condition": "failed（图像基线）", "mechanism": "无文字层且OCR未启用", "impact": "无法获得完整证据", "treatment": "归入OCR需求，不解释为LLM错误"},
        {"condition": "外部服务不可达", "mechanism": "DNS/网络错误", "impact": "没有有效LLM响应", "treatment": "排除出算法性能统计"},
    ]


def _manuscript_result_text(rows: list[dict[str, Any]], llm: dict[str, Any] | None) -> str:
    xlsx = next(row for row in rows if row["carrier"] == "xlsx")
    json_row = next(row for row in rows if row["carrier"] == "json")
    return (
        "在4组匿名工程案例和10种输入载体上共完成40次确定性提取基线运行。"
        f"规范化XLSX的参数Precision为{xlsx['mean_precision_pct']:.1f}%，Recall为{xlsx['mean_recall_pct']:.1f}%；"
        f"虽然严格参数集完整等价率为{xlsx['parameter_equivalence_pct']:.1f}%，但在补齐预先声明的编译默认值后，"
        f"4个案例均生成了与参考模型节点数、S4R单元数及静力/模态INP签名一致的模型，模型等价率为{xlsx['model_equivalence_pct']:.1f}%。"
        f"JSON的参数Recall为{json_row['mean_recall_pct']:.1f}%，表明嵌套键路径能够保留大部分活动参数；"
        "然而，少量必需字段缺失仍触发参数门禁，说明字段级恢复率不能替代正式参数集完整性检查。"
        + (
            "完整LLM模式结果已另行纳入统计。" if llm else
            "由于外部LLM服务在实验窗口内DNS不可达，本阶段未获得可用于算法比较的LLM结果，相关结论留待网络恢复后按冻结配置补充。"
        )
    )


def _html_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                rendered = f"{value:.3f}".rstrip("0").rstrip(".")
            else:
                rendered = str(value)
            cells.append(f"<td>{html.escape(rendered)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def _failure_label(value: str) -> str:
    return {
        "none": "通过",
        "blocked": "参数门禁阻断",
        "parameter_mismatch": "参数不完整",
        "confirmation_required": "需要确认",
        "parameter_conflict": "参数冲突",
        "failed": "提取失败",
    }.get(value, value)


def _validate_dataset(manifest: dict[str, Any], deterministic: dict[str, Any]) -> None:
    if manifest.get("case_count") != 4 or manifest.get("condition_count") != 40:
        raise ValueError("G3-E manifest must contain 4 cases and 40 carrier conditions.")
    if deterministic.get("run_count") != 40:
        raise ValueError("Deterministic baseline must contain exactly 40 runs.")
    observed = Counter((row["case_id"], row["carrier"]) for row in deterministic["runs"])
    if len(observed) != 40 or set(observed.values()) != {1}:
        raise ValueError("Deterministic baseline must contain one run per case and carrier.")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            })


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _report_css() -> str:
    return """
@page{size:A4;margin:16mm 17mm}*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:#edf2f4;color:#1e2d35;font-family:"Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif;font-size:14px;line-height:1.75}
main{max-width:1100px;margin:24px auto;background:#fff;box-shadow:0 12px 40px #1c40551a;padding:0 58px 50px}
header{margin:0 -58px;padding:42px 58px 34px;background:linear-gradient(125deg,#153d58,#14787a);color:white}
.eyebrow{font-size:12px;letter-spacing:.16em;color:#8fe2df}.subtitle{font-size:17px;margin:4px 0 20px;color:#d9f1f0}
h1{font-size:30px;line-height:1.3;margin:8px 0 4px}h2{font-size:22px;color:#16415d;border-bottom:2px solid #d9e7eb;padding-bottom:8px;margin-top:38px}
h3{font-size:16px;color:#24566f;margin-top:26px}.meta{display:flex;gap:10px;flex-wrap:wrap}.meta span{background:#ffffff18;border:1px solid #ffffff33;border-radius:20px;padding:4px 12px}
.status{margin:22px 0;padding:13px 16px;border-left:5px solid;border-radius:5px}.status.warn{background:#fff7e9;border-color:#d98545}.status.ok{background:#e9f7f3;border-color:#2f8f83}
nav{position:sticky;top:0;z-index:5;margin:0 -58px;padding:10px 58px;background:#fffefef2;border-bottom:1px solid #dbe5e8;backdrop-filter:blur(8px)}nav a{color:#24566f;text-decoration:none;margin-right:20px;font-weight:600}
.key-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.key-grid div{padding:18px 12px;background:#f1f6f7;border-radius:8px;text-align:center}.key-grid b{display:block;font-size:25px;color:#14787a}.key-grid span{font-size:12px;color:#647780}
.figure{margin:24px 0 10px;border:1px solid #dfe8eb;padding:12px 12px 4px;border-radius:7px}.figure svg{width:100%;height:auto;display:block}.caption{font-family:SimSun,"Songti SC",serif;font-size:12px;line-height:1.55;color:#40545e;margin:8px 4px 10px}
.formula{background:#f4f7f8;border-left:4px solid #65afc1;padding:12px 16px;margin:18px 0;font-family:Cambria,serif}.table-wrap{overflow-x:auto;margin:12px 0 22px}table{border-collapse:collapse;width:100%;font-size:12px}th{background:#16415d;color:white;font-weight:600}th,td{border:1px solid #cddade;padding:7px 8px;text-align:center}tbody tr:nth-child(even){background:#f5f8f9}
.paper-text{padding:18px 22px;background:#fbfaf6;border:1px solid #e2ddd0;font-family:SimSun,"Songti SC",serif;text-align:justify}.paper-text h3{margin-top:0;text-align:center;color:#222}
footer{margin-top:42px;padding-top:18px;border-top:1px solid #dce5e8;text-align:center;color:#809098;font-size:11px}
@media(max-width:760px){main{margin:0;padding:0 18px 36px}header,nav{margin-left:-18px;margin-right:-18px;padding-left:18px;padding-right:18px}.key-grid{grid-template-columns:repeat(2,1fr)}h1{font-size:24px}}
@media print{body{background:white;font-size:10.5pt}main{max-width:none;margin:0;box-shadow:none;padding:0}nav{display:none}header{margin:0 0 18px;padding:24px 28px}.figure{break-inside:avoid}.table-wrap{break-inside:avoid}h2{break-after:avoid}}
"""


if __name__ == "__main__":
    raise SystemExit(main())
