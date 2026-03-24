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
