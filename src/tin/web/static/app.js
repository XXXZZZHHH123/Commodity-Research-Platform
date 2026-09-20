// 页面交互：主题、口径穿透弹层、快速录入抽屉、提示条、合约表与期限结构联动。
// 页面切换走服务端路由，不在前端做 SPA。

(function () {
  const saved = localStorage.getItem("tin-theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (saved === "dark" || (saved === null && prefersDark)) document.documentElement.classList.add("dark");
})();

function toggleTheme() {
  const dark = document.documentElement.classList.toggle("dark");
  localStorage.setItem("tin-theme", dark ? "dark" : "light");
}

function togglePopover(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const hidden = el.classList.contains("hidden");
  document.querySelectorAll("[data-popover]").forEach((p) => p.classList.add("hidden"));
  if (hidden) el.classList.remove("hidden");
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-popover], [data-popover-trigger]")) return;
  document.querySelectorAll("[data-popover]").forEach((p) => p.classList.add("hidden"));
});

function toggleDrawer(open) {
  const drawer = document.getElementById("quick-drawer");
  const backdrop = document.getElementById("quick-drawer-backdrop");
  if (!drawer) return;
  drawer.classList.toggle("translate-x-full", !open);
  backdrop.classList.toggle("hidden", !open);
  if (open) setTimeout(() => document.getElementById("drawer-value").focus(), 320);
}

function openDrawerWith(seriesId) {
  const select = document.getElementById("drawer-series");
  if (select) {
    select.value = seriesId;
    syncDrawerUnit();
  }
  toggleDrawer(true);
}

function syncDrawerUnit() {
  const select = document.getElementById("drawer-series");
  const unit = document.getElementById("drawer-unit");
  const asOf = document.getElementById("drawer-as-of");
  if (!select || !unit) return;
  const opt = select.selectedOptions[0];
  unit.innerText = opt.dataset.unit || "";
  // 数据时点默认填该指标的常规发布时刻，提醒填的是数据代表时点而非录入时刻
  if (asOf && opt.dataset.asOf && !asOf.dataset.touched) asOf.value = opt.dataset.asOf;
}

function showToast(msg) {
  const toast = document.getElementById("toast");
  const text = document.getElementById("toast-msg");
  if (!toast || !text) return;
  text.innerText = msg;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 3500);
}

function copyText(txt) {
  navigator.clipboard.writeText(txt).then(
    () => showToast("已复制指标代码：" + txt),
    () => showToast("复制失败，请手动选择"),
  );
}

function highlightContract(contract) {
  const point = document.querySelector('[data-contract-point="' + contract + '"]');
  const label = document.getElementById("chart-hover-indicator");
  document.querySelectorAll("[data-contract-point]").forEach((c) => {
    c.setAttribute("r", c.dataset.main === "1" ? "5.5" : "4");
  });
  if (point) {
    point.setAttribute("r", "7");
    if (label) label.innerText = point.dataset.caption;
  }
}

function resetContract() {
  const label = document.getElementById("chart-hover-indicator");
  document.querySelectorAll("[data-contract-point]").forEach((c) => {
    c.setAttribute("r", c.dataset.main === "1" ? "5.5" : "4");
  });
  const main = document.querySelector('[data-contract-point][data-main="1"]');
  if (label && main) label.innerText = main.dataset.caption;
}

document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("msg")) showToast(params.get("msg"));
  const asOf = document.getElementById("drawer-as-of");
  if (asOf) asOf.addEventListener("input", () => { asOf.dataset.touched = "1"; });
  syncDrawerUnit();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleDrawer(false);
  });
});
