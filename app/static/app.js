const urlInput = document.getElementById("url");
const fetchInfoBtn = document.getElementById("fetchInfoBtn");
const videoInfo = document.getElementById("videoInfo");
const statusEl = document.getElementById("status");
const downloadBtn = document.getElementById("downloadBtn");
const qualitySelect = document.getElementById("qualitySelect");
const formatSelect = document.getElementById("formatSelect");
const startSlider = document.getElementById("startSlider");
const endSlider = document.getElementById("endSlider");
const trimReadout = document.getElementById("trimReadout");
const filenamePrefix = document.getElementById("filenamePrefix");
const enableTrim = document.getElementById("enableTrim");
const trimPanel = document.getElementById("trimPanel");
const themeToggleBtn = document.getElementById("themeToggleBtn");
const funLoader = document.getElementById("funLoader");
const funLoaderText = document.getElementById("funLoaderText");
const toastContainer = document.getElementById("toastContainer");

let currentDuration = 0;
const defaultVideoQualities = ["best", "1080", "720", "480", "360"];
const defaultAudioQualities = ["320", "256", "192", "128"];
let videoQualities = [...defaultVideoQualities];
let audioQualities = [...defaultAudioQualities];
let videoFormats = ["mp4", "webm"];
let audioFormats = ["mp3", "m4a", "wav"];

function setStatus(message, type = "") {
  statusEl.textContent = message;
  statusEl.className = "status";
  if (type) {
    statusEl.classList.add(type);
  }
  if (type === "error" || type === "success") {
    showToast(message, type);
  }
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("hide");
    setTimeout(() => toast.remove(), 250);
  }, 3200);
}

function setButtonLoading(button, isLoading, loadingText, idleText) {
  button.disabled = isLoading;
  button.textContent = isLoading ? loadingText : idleText;
}

function showFunLoader(text) {
  funLoaderText.textContent = text || "Processing request...";
  funLoader.classList.remove("hidden");
}

function hideFunLoader() {
  funLoader.classList.add("hidden");
}

function applyTheme(theme) {
  const normalized = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", normalized);
  localStorage.setItem("clipfetch-theme", normalized);
  themeToggleBtn.textContent = normalized === "dark" ? "Light Mode" : "Dark Mode";
}

function getMode() {
  const selected = document.querySelector("input[name='mode']:checked");
  return selected ? selected.value : "video";
}

function formatSeconds(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds || 0)));
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hours > 0) {
    return `${hours}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function renderQualityOptions() {
  const mode = getMode();
  const options = mode === "audio" ? audioQualities : videoQualities;
  qualitySelect.innerHTML = options
    .map((q) => {
      if (mode === "audio") {
        return `<option value="${q}">${q} kbps</option>`;
      }
      return `<option value="${q}">${q === "best" ? "Best available" : `${q}p`}</option>`;
    })
    .join("");
}

function renderFormatOptions() {
  const mode = getMode();
  const options = mode === "audio" ? audioFormats : videoFormats;
  formatSelect.innerHTML = options.map((fmt) => `<option value="${fmt}">${fmt.toUpperCase()}</option>`).join("");
}

function syncTrimDisplay() {
  const startValue = Number(startSlider.value || 0);
  const endValue = Number(endSlider.value || 0);
  trimReadout.textContent = `${formatSeconds(startValue)} - ${formatSeconds(endValue)}`;
}

function initSliders(duration) {
  currentDuration = Math.max(0, Math.floor(Number(duration || 0)));
  startSlider.max = String(currentDuration);
  endSlider.max = String(currentDuration);
  startSlider.value = "0";
  endSlider.value = String(currentDuration);
  syncTrimDisplay();
}

async function fetchVideoInfo() {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Please enter a media URL.", "error");
    return;
  }

  setStatus("Fetching video info...", "loading");
  videoInfo.classList.add("hidden");
  setButtonLoading(fetchInfoBtn, true, "Loading...", "Fetch Video Info");
  showFunLoader("Fetching media details...");

  try {
    const response = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to fetch video info.");
    }

    const info = data.info;
    videoQualities = info.video_qualities || defaultVideoQualities;
    audioQualities = info.audio_qualities || defaultAudioQualities;
    videoFormats = info.video_formats || ["mp4", "webm"];
    audioFormats = info.audio_formats || ["mp3", "m4a", "wav"];
    const duration = Number(info.duration || 0);
    const mins = Math.floor(duration / 60);
    const secs = duration % 60;
    const formattedDuration = `${mins}:${String(secs).padStart(2, "0")}`;

    videoInfo.innerHTML = `
      <img src="${info.thumbnail || ""}" alt="Thumbnail" />
      <div>
        <p><strong>Title:</strong> ${info.title || "Unknown"}</p>
        <p><strong>Uploader:</strong> ${info.uploader || "Unknown"}</p>
        <p><strong>Duration:</strong> ${formattedDuration}</p>
      </div>
    `;
    videoInfo.classList.remove("hidden");
    initSliders(duration);
    renderQualityOptions();
    renderFormatOptions();
    setStatus("Video info loaded.", "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setButtonLoading(fetchInfoBtn, false, "Loading...", "Fetch Video Info");
    hideFunLoader();
  }
}

function downloadMedia() {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Please enter a media URL.", "error");
    return;
  }

  const mode = getMode();
  const quality = qualitySelect.value;
  const outputFormat = formatSelect.value;
  const trimEnabled = enableTrim.checked;
  const start = Number(startSlider.value || 0);
  const end = Number(endSlider.value || 0);

  if (trimEnabled && currentDuration > 0 && end <= start) {
    setStatus("End time must be greater than start time.", "error");
    return;
  }

  const fullStart = !trimEnabled || start <= 0;
  const fullEnd = !trimEnabled || (currentDuration > 0 ? end >= currentDuration : true);

  const params = new URLSearchParams({
    url,
    mode,
    quality,
    output_format: outputFormat,
    include_subtitles: "false",
    include_thumbnail: "false",
    filename_prefix: filenamePrefix.value.trim(),
    start: fullStart ? "" : String(start),
    end: fullEnd ? "" : String(end),
  });

  setButtonLoading(downloadBtn, true, "Preparing download...", "Download");
  setStatus("Preparing download, this may take a few seconds...", "loading");
  showFunLoader("Preparing your download...");
  const downloadUrl = `/api/download?${params.toString()}`;
  const a = document.createElement("a");
  a.href = downloadUrl;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => {
    setButtonLoading(downloadBtn, false, "Preparing download...", "Download");
    setStatus("Download started. Check your downloads folder.", "success");
    hideFunLoader();
  }, 4500);
}

startSlider.addEventListener("input", () => {
  if (Number(startSlider.value) >= Number(endSlider.value)) {
    startSlider.value = String(Math.max(0, Number(endSlider.value) - 1));
  }
  syncTrimDisplay();
});

endSlider.addEventListener("input", () => {
  if (Number(endSlider.value) <= Number(startSlider.value)) {
    endSlider.value = String(Math.min(currentDuration, Number(startSlider.value) + 1));
  }
  syncTrimDisplay();
});

document.querySelectorAll("input[name='mode']").forEach((el) => {
  el.addEventListener("change", () => {
    renderQualityOptions();
    renderFormatOptions();
  });
});
enableTrim.addEventListener("change", () => {
  trimPanel.classList.toggle("hidden", !enableTrim.checked);
});

fetchInfoBtn.addEventListener("click", fetchVideoInfo);
downloadBtn.addEventListener("click", downloadMedia);
themeToggleBtn.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  applyTheme(current === "dark" ? "light" : "dark");
});
renderQualityOptions();
renderFormatOptions();
applyTheme(localStorage.getItem("clipfetch-theme") || "light");

// ── Tab switching ──────────────────────────────────────────────
const tabBtns = document.querySelectorAll(".tab-btn");
const downloaderTabEl = document.getElementById("downloaderTab");
const scraperTabEl = document.getElementById("scraperTab");

tabBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    tabBtns.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    downloaderTabEl.classList.toggle("hidden", tab !== "downloader");
    scraperTabEl.classList.toggle("hidden", tab !== "scraper");
  });
});

// ── Page Scraper ───────────────────────────────────────────────
const scrapeUrlInput = document.getElementById("scrapeUrl");
const scanBtn = document.getElementById("scanBtn");
const scraperResults = document.getElementById("scraperResults");
const scraperFilters = document.getElementById("scraperFilters");
const mediaGrid = document.getElementById("mediaGrid");
const downloadSelectedBtn = document.getElementById("downloadSelectedBtn");
const scraperStatusEl = document.getElementById("scraperStatus");

let allMedia = [];
let activeFilter = "all";

const TYPE_LABELS = { image: "Image", video: "Video", audio: "Audio", document: "Document", cad: "CAD", archive: "Archive" };
const TYPE_ICONS  = { image: "IMG", video: "VID", audio: "AUD", document: "PDF", cad: "CAD", archive: "ZIP" };

function setScraperStatus(msg, type = "") {
  scraperStatusEl.textContent = msg;
  scraperStatusEl.className = "status" + (type ? " " + type : "");
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderFilters() {
  const counts = {};
  allMedia.forEach((m) => { counts[m.type] = (counts[m.type] || 0) + 1; });
  const types = Object.keys(counts);

  scraperFilters.innerHTML = [
    `<button class="filter-btn${activeFilter === "all" ? " active" : ""}" data-filter="all">All (${allMedia.length})</button>`,
    ...types.map((t) => `<button class="filter-btn${activeFilter === t ? " active" : ""}" data-filter="${t}">${TYPE_LABELS[t] || t} (${counts[t]})</button>`),
  ].join("");

  scraperFilters.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      activeFilter = btn.dataset.filter;
      renderFilters();
      renderGrid();
    });
  });
}

function renderGrid() {
  const items = activeFilter === "all" ? allMedia : allMedia.filter((m) => m.type === activeFilter);
  if (items.length === 0) {
    mediaGrid.innerHTML = `<p class="scraper-empty">No ${activeFilter === "all" ? "" : (TYPE_LABELS[activeFilter] || activeFilter) + " "}files found.</p>`;
    return;
  }
  mediaGrid.innerHTML = items
    .map((item) => {
      const icon = TYPE_ICONS[item.type] || "FILE";
      const preview =
        item.type === "image"
          ? `<img src="${escapeHtml(item.url)}" alt="" loading="lazy" onerror="this.parentElement.innerHTML='<span class=media-icon>${icon}</span>'" />`
          : `<span class="media-icon">${icon}</span>`;
      return `<div class="media-item" data-url="${escapeHtml(item.url)}" data-name="${escapeHtml(item.name)}">
        <label class="media-check-wrap"><input type="checkbox" class="media-item-check" /></label>
        <div class="media-preview">${preview}</div>
        <div class="media-meta">
          <span class="media-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
          <span class="type-badge type-${item.type}">${TYPE_LABELS[item.type] || item.type}</span>
        </div>
      </div>`;
    })
    .join("");
}

async function scanPage() {
  const url = scrapeUrlInput.value.trim();
  if (!url) { setScraperStatus("Please enter a URL.", "error"); return; }

  setScraperStatus("Scanning page...", "loading");
  scanBtn.disabled = true;
  scanBtn.textContent = "Scanning...";
  scraperResults.classList.add("hidden");
  showFunLoader("Scanning page for media...");

  try {
    const res = await fetch("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Scan failed.");

    allMedia = data.media || [];
    activeFilter = "all";

    if (allMedia.length === 0) {
      setScraperStatus("No media files found on this page.", "error");
    } else {
      setScraperStatus(`Found ${allMedia.length} media file${allMedia.length !== 1 ? "s" : ""}.`, "success");
      scraperResults.classList.remove("hidden");
      renderFilters();
      renderGrid();
    }
  } catch (err) {
    setScraperStatus(err.message, "error");
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = "Scan Page";
    hideFunLoader();
  }
}

async function downloadSelected() {
  const checked = [...document.querySelectorAll(".media-item-check:checked")];
  if (checked.length === 0) { setScraperStatus("Select at least one file to download.", "error"); return; }

  downloadSelectedBtn.disabled = true;
  setScraperStatus(`Downloading ${checked.length} file${checked.length !== 1 ? "s" : ""}...`, "loading");

  for (let i = 0; i < checked.length; i++) {
    const item = checked[i].closest(".media-item");
    const fileUrl = item.dataset.url;
    const name = item.dataset.name;
    const safeUrl = fileUrl.split("").map((c) => c.charCodeAt(0) > 127 ? encodeURIComponent(c) : c).join("");
    const safeName = name.split("").map((c) => c.charCodeAt(0) > 127 ? encodeURIComponent(c) : c).join("");
    const proxyUrl = `/api/proxy-download?url=${encodeURIComponent(safeUrl)}&name=${encodeURIComponent(safeName)}`;
    const a = document.createElement("a");
    a.href = proxyUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    if (i < checked.length - 1) await new Promise((r) => setTimeout(r, 600));
  }

  setScraperStatus("Downloads started. Check your downloads folder.", "success");
  downloadSelectedBtn.disabled = false;
}

scanBtn.addEventListener("click", scanPage);
scrapeUrlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") scanPage(); });
downloadSelectedBtn.addEventListener("click", downloadSelected);
document.getElementById("selectAllBtn").addEventListener("click", () => {
  document.querySelectorAll(".media-item-check").forEach((cb) => (cb.checked = true));
});
document.getElementById("deselectAllBtn").addEventListener("click", () => {
  document.querySelectorAll(".media-item-check").forEach((cb) => (cb.checked = false));
});
