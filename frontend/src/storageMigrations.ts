const LEGACY_PDF_API_SETTINGS_STORAGE_KEY = "polyprop.pdfSimilarityDemo.apiSettings";

export function runStorageMigrations(): void {
  try {
    window.localStorage.removeItem(LEGACY_PDF_API_SETTINGS_STORAGE_KEY);
  } catch {
    // Storage can be unavailable or blocked by the browser. Migrations must not block startup.
  }
}
