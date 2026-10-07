/// <reference types="vite/client" />

interface WindTowerDesktopUpdate {
  version: string;
  notes?: string;
  download_url: string;
  sha256?: string;
}

interface WindTowerDesktopApi {
  runtime: {
    desktop: true;
    apiBase: string;
    appVersion: string;
    platform: string;
  };
  getRuntimeInfo(): Promise<Record<string, unknown>>;
  chooseFiles(): Promise<string[]>;
  openDataFolder(): Promise<void>;
  openLogs(): Promise<void>;
  checkForUpdates(): Promise<{ available: boolean; update?: WindTowerDesktopUpdate; reason?: string }>;
  downloadUpdate(update: WindTowerDesktopUpdate): Promise<{ downloaded: boolean; path?: string }>;
  saveLlmKey(apiKey: string): Promise<{ configured: boolean }>;
  quitApplication(): Promise<{ quitting: boolean; activeCount?: number }>;
  onUpdateAvailable(callback: (update: WindTowerDesktopUpdate) => void): () => void;
}

interface Window {
  windTowerDesktop?: WindTowerDesktopApi;
}
