import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

import type {
  HighlightMode,
  MeshData,
  OdbResultMesh,
  ResultScalar,
  ViewMode,
} from "../../types";

interface ModelViewportProps {
  meshDataUrl?: string;
  resultMesh?: OdbResultMesh | null;
  mode: "mesh" | "result";
  viewMode: ViewMode;
  highlight: HighlightMode;
  scalar: ResultScalar;
  deformationScale: number;
  sourceLabel?: string;
  warnings?: string[];
  onViewModeChange?: (mode: ViewMode) => void;
  onError?: (message: string) => void;
}

interface DisplayBounds {
  center: THREE.Vector3;
  radius: number;
}

export function ModelViewport({
  meshDataUrl,
  resultMesh,
  mode,
  viewMode,
  highlight,
  scalar,
  deformationScale,
  sourceLabel,
  warnings = [],
  onViewModeChange,
  onError,
}: ModelViewportProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [meshData, setMeshData] = useState<MeshData | null>(null);
  const [loadError, setLoadError] = useState("");
  const [viewerWarning, setViewerWarning] = useState("");
  const displayData = useMemo(
    () => (mode === "result" ? resultMesh ?? null : meshData),
    [meshData, mode, resultMesh],
  );
  const warningText = [viewerWarning, ...warnings].filter(Boolean).join(" · ");

  useEffect(() => {
    if (!meshDataUrl) {
      setMeshData(null);
      return;
    }
    const controller = new AbortController();
    setLoadError("");
    fetch(meshDataUrl, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("无法读取网格数据");
        return response.json() as Promise<MeshData>;
      })
      .then(setMeshData)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        const detail = reason instanceof Error ? reason.message : String(reason);
        const nextError = `网格数据加载失败：${detail}`;
        setLoadError(nextError);
        onError?.(nextError);
      });
    return () => controller.abort();
  }, [meshDataUrl, onError]);

  useEffect(() => {
    if (!hostRef.current || !displayData) return;
    return renderModel(hostRef.current, displayData, {
      mode,
      viewMode,
      highlight,
      scalar,
      deformationScale,
      setViewerWarning,
    });
  }, [deformationScale, displayData, highlight, mode, scalar, viewMode]);

  if (!displayData) {
    return (
      <div className="model-viewport-shell model-viewport-empty">
        <div className="viewport-empty-graphic" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <strong>
          {loadError ||
            (mode === "result" ? "ODB 结果网格尚未生成" : "确认参数后生成网格预览")}
        </strong>
        <small>视图支持整体、门洞局部及四种工程视角</small>
      </div>
    );
  }

  return (
    <div className="model-viewport-shell">
      <div className="model-viewport-host" ref={hostRef} />
      <div className="axis-navigator" aria-label="工程视角导航">
        <button
          className={viewMode === "xz" ? "active x-axis" : "x-axis"}
          onClick={() => onViewModeChange?.("xz")}
          title="正视图"
          type="button"
        >
          X
        </button>
        <button
          className={viewMode === "yz" ? "active y-axis" : "y-axis"}
          onClick={() => onViewModeChange?.("yz")}
          title="侧视图"
          type="button"
        >
          Y
        </button>
        <button
          className={viewMode === "xy" ? "active z-axis" : "z-axis"}
          onClick={() => onViewModeChange?.("xy")}
          title="俯视图"
          type="button"
        >
          Z
        </button>
      </div>
      <div className="viewport-source">{sourceLabel || (mode === "result" ? "结果云图" : "网格预览")}</div>
      {warningText ? <div className="viewport-warning">{warningText}</div> : null}
    </div>
  );
}

function renderModel(
  host: HTMLDivElement,
  data: MeshData,
  options: {
    mode: "mesh" | "result";
    viewMode: ViewMode;
    highlight: HighlightMode;
    scalar: ResultScalar;
    deformationScale: number;
    setViewerWarning: (message: string) => void;
  },
) {
  host.replaceChildren();
  const scene = new THREE.Scene();
  scene.background = new THREE.Color("#eef4f7");
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(host.clientWidth, host.clientHeight);
  host.appendChild(renderer.domElement);

  const bounds = data.bounds ?? computeBounds(data.points);
  const center = [
    ((bounds.min[0] ?? 0) + (bounds.max[0] ?? 0)) * 0.5,
    ((bounds.min[1] ?? 0) + (bounds.max[1] ?? 0)) * 0.5,
    ((bounds.min[2] ?? 0) + (bounds.max[2] ?? 0)) * 0.5,
  ];
  const span = Math.max(
    Math.abs((bounds.max[0] ?? 0) - (bounds.min[0] ?? 0)),
    Math.abs((bounds.max[1] ?? 0) - (bounds.min[1] ?? 0)),
    Math.abs((bounds.max[2] ?? 0) - (bounds.min[2] ?? 0)),
    1,
  );
  const scale = 8 / span;
  const points = resultPoints(data, options.mode === "result" ? options.deformationScale : 0);
  const positions: number[] = [];
  const colors: number[] = [];
  const linePositions: number[] = [];
  const lineColors: number[] = [];
  const fullBox = new THREE.Box3();
  const doorBox = new THREE.Box3();
  const flangeBox = new THREE.Box3();
  const contactBox = new THREE.Box3();
  const boltBox = new THREE.Box3();
  const featureCodes = numbers(data.cell_data?.feature_code);
  const cellScalar = options.scalar === "S_Mises" ? numbers(data.cell_data?.S_Mises) : [];
  const pointScalar = options.scalar === "U_mag" ? numbers(data.point_data?.U_mag) : [];
  const cellRange = valueRange(cellScalar);
  const pointRange = valueRange(pointScalar);
  let doorCellCount = 0;

  data.quads.forEach((quad, cellIndex) => {
    if (quad.length < 4) return;
    const featureCode = featureCodes[cellIndex] ?? 0;
    const isDoor = Boolean(featureCode & 1);
    const isFlange = Boolean(featureCode & 2);
    const isContact = Boolean(featureCode & 4);
    if (isDoor) doorCellCount += 1;
    const value = options.scalar === "S_Mises"
      ? cellScalar[cellIndex] ?? 0
      : meanQuadValue(quad, pointScalar);
    const color = options.mode === "result"
      ? colorForScalar(value, options.scalar === "S_Mises" ? cellRange : pointRange)
      : colorForFeature(featureCode, options.highlight);

    [quad[0], quad[1], quad[2], quad[0], quad[2], quad[3]].forEach((pointIndex) => {
      const point = mapPoint(points[pointIndex], center, scale);
      positions.push(point.x, point.y, point.z);
      colors.push(color.r, color.g, color.b);
      fullBox.expandByPoint(point);
      if (isDoor) doorBox.expandByPoint(point);
      if (isFlange) flangeBox.expandByPoint(point);
      if (isContact) contactBox.expandByPoint(point);
    });
    for (let index = 0; index < 4; index += 1) {
      const first = mapPoint(points[quad[index]], center, scale);
      const second = mapPoint(points[quad[(index + 1) % 4]], center, scale);
      const edgeColor = isDoor || options.highlight === "mesh"
        ? new THREE.Color("#20364a")
        : new THREE.Color("#b8c7d3");
      linePositions.push(first.x, first.y, first.z, second.x, second.y, second.z);
      lineColors.push(
        edgeColor.r,
        edgeColor.g,
        edgeColor.b,
        edgeColor.r,
        edgeColor.g,
        edgeColor.b,
      );
    }
  });

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  geometry.computeVertexNormals();
  const material = new THREE.MeshStandardMaterial({
    vertexColors: true,
    roughness: 0.58,
    metalness: 0.04,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.97,
  });
  scene.add(new THREE.Mesh(geometry, material));

  const edgeGeometry = new THREE.BufferGeometry();
  edgeGeometry.setAttribute("position", new THREE.Float32BufferAttribute(linePositions, 3));
  edgeGeometry.setAttribute("color", new THREE.Float32BufferAttribute(lineColors, 3));
  const edgeMaterial = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.34,
  });
  scene.add(new THREE.LineSegments(edgeGeometry, edgeMaterial));

  const boltPositions: number[] = [];
  for (const line of data.lines ?? []) {
    if (line.length < 2) continue;
    const first = mapPoint(points[line[0]], center, scale);
    const second = mapPoint(points[line[1]], center, scale);
    boltPositions.push(first.x, first.y, first.z, second.x, second.y, second.z);
    fullBox.expandByPoint(first);
    fullBox.expandByPoint(second);
    boltBox.expandByPoint(first);
    boltBox.expandByPoint(second);
  }
  const boltGeometry = new THREE.BufferGeometry();
  boltGeometry.setAttribute("position", new THREE.Float32BufferAttribute(boltPositions, 3));
  const boltMaterial = new THREE.LineBasicMaterial({
    color: options.highlight === "bolts" ? "#e4572e" : "#7b4f32",
    linewidth: 2,
    transparent: true,
    opacity: options.highlight === "bolts" ? 1 : 0.82,
  });
  scene.add(new THREE.LineSegments(boltGeometry, boltMaterial));

  scene.add(new THREE.HemisphereLight("#ffffff", "#7892a4", 2.15));
  const keyLight = new THREE.DirectionalLight("#ffffff", 1.65);
  keyLight.position.set(4, 8, 5);
  scene.add(keyLight);
  scene.add(new THREE.GridHelper(10, 10, "#9db0bd", "#d5e0e6"));

  const camera = new THREE.PerspectiveCamera(
    35,
    Math.max(host.clientWidth, 1) / Math.max(host.clientHeight, 1),
    0.01,
    1000,
  );
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  const selectedBox = options.highlight === "door"
    ? doorBox
    : options.highlight === "flange"
      ? flangeBox
      : options.highlight === "contact"
        ? contactBox
        : options.highlight === "bolts"
          ? boltBox
          : fullBox;
  const targetBounds = selectedBox.isEmpty() ? boundsFromBox(fullBox) : boundsFromBox(selectedBox);
  if (options.highlight === "door" && doorCellCount === 0) {
    options.setViewerWarning("未检测到门洞局部单元，已保持整体视图");
  } else {
    options.setViewerWarning("");
  }
  applyCameraView(camera, controls, options.viewMode, targetBounds);

  const label = document.createElement("div");
  label.className = "model-viewport-stats";
  const scalarRange = options.scalar === "S_Mises" ? cellRange : pointRange;
  label.textContent = options.mode === "result"
    ? `${options.scalar} · max ${formatNumber(scalarRange.max)} · nodes ${data.points.length.toLocaleString()} · S4R ${data.quads.length.toLocaleString()}`
    : `nodes ${data.points.length.toLocaleString()} · S4R ${data.quads.length.toLocaleString()} · B31 ${(data.lines?.length ?? 0).toLocaleString()} · door ${doorCellCount.toLocaleString()}`;
  host.appendChild(label);

  let frame = 0;
  const animate = () => {
    frame = requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  };
  animate();

  const resize = () => {
    if (!host.clientWidth || !host.clientHeight) return;
    camera.aspect = host.clientWidth / host.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(host.clientWidth, host.clientHeight);
  };
  const observer = new ResizeObserver(resize);
  observer.observe(host);

  return () => {
    cancelAnimationFrame(frame);
    observer.disconnect();
    controls.dispose();
    geometry.dispose();
    edgeGeometry.dispose();
    boltGeometry.dispose();
    material.dispose();
    edgeMaterial.dispose();
    boltMaterial.dispose();
    renderer.dispose();
    host.replaceChildren();
  };
}

function resultPoints(data: MeshData, deformationScale: number) {
  if (!data.deformed_points || deformationScale <= 0) return data.points;
  return data.points.map((point, index) => {
    const deformed = data.deformed_points?.[index] ?? point;
    return [
      point[0] + (deformed[0] - point[0]) * deformationScale,
      point[1] + (deformed[1] - point[1]) * deformationScale,
      point[2] + (deformed[2] - point[2]) * deformationScale,
    ];
  });
}

function applyCameraView(
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
  viewMode: ViewMode,
  bounds: DisplayBounds,
) {
  const target = bounds.center;
  const distance = Math.max(bounds.radius * 2.4, 4);
  camera.up.set(0, 1, 0);
  if (viewMode === "xz") camera.position.set(target.x + distance, target.y, target.z);
  else if (viewMode === "yz") camera.position.set(target.x, target.y, target.z + distance);
  else if (viewMode === "xy") {
    camera.position.set(target.x, target.y + distance, target.z + 0.001);
    camera.up.set(0, 0, -1);
  } else {
    camera.position.set(
      target.x + distance * 0.85,
      target.y + distance * 0.52,
      target.z + distance * 0.92,
    );
  }
  camera.near = Math.max(distance / 200, 0.01);
  camera.far = Math.max(distance * 20, 1000);
  camera.lookAt(target);
  camera.updateProjectionMatrix();
  controls.target.copy(target);
  controls.update();
}

function mapPoint(point: number[] | undefined, center: number[], scale: number) {
  return new THREE.Vector3(
    ((point?.[0] ?? 0) - center[0]) * scale,
    ((point?.[2] ?? 0) - center[2]) * scale,
    ((point?.[1] ?? 0) - center[1]) * scale,
  );
}

function colorForFeature(code: number, highlight: HighlightMode) {
  if (highlight === "door" && !(code & 1)) return new THREE.Color("#d4dee7");
  if (highlight === "flange" && !(code & 2)) return new THREE.Color("#d4dee7");
  if (highlight === "contact" && !(code & 4)) return new THREE.Color("#d4dee7");
  if (highlight === "bolts") return new THREE.Color("#d4dee7");
  if (highlight === "flange" && (code & 2)) return new THREE.Color("#19a2a6");
  if (highlight === "contact" && (code & 4)) return new THREE.Color("#f2a65a");
  if (code & 4) return new THREE.Color("#dc3545");
  if (code & 1) return new THREE.Color("#f27a2c");
  return new THREE.Color("#4f93cf");
}

function colorForScalar(value: number, scalarRange: { min: number; max: number }) {
  const progress = scalarRange.max > scalarRange.min
    ? (value - scalarRange.min) / (scalarRange.max - scalarRange.min)
    : 0;
  return new THREE.Color().setHSL(
    0.62 - 0.62 * Math.max(0, Math.min(1, progress)),
    0.86,
    0.52,
  );
}

function computeBounds(points: number[][]) {
  if (!points.length) return { min: [0, 0, 0], max: [1, 1, 1] };
  return {
    min: [0, 1, 2].map((index) => Math.min(...points.map((point) => point[index] ?? 0))),
    max: [0, 1, 2].map((index) => Math.max(...points.map((point) => point[index] ?? 0))),
  };
}

function boundsFromBox(box: THREE.Box3): DisplayBounds {
  const center = new THREE.Vector3();
  const size = new THREE.Vector3();
  box.getCenter(center);
  box.getSize(size);
  return { center, radius: Math.max(size.length() * 0.5, 0.5) };
}

function numbers(value: unknown): number[] {
  return Array.isArray(value) ? value.map((item) => Number(item) || 0) : [];
}

function valueRange(values: number[]) {
  return {
    min: values.length ? Math.min(...values) : 0,
    max: values.length ? Math.max(...values) : 0,
  };
}

function meanQuadValue(quad: number[], values: number[]) {
  const selected = quad.map((index) => values[index] ?? 0);
  return selected.reduce((sum, value) => sum + value, 0) / Math.max(selected.length, 1);
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}
