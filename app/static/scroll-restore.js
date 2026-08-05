/*
 * Forms in the reader deliberately use POST + redirect so a refresh never
 * resubmits an action. Browsers reset the scroll position during that
 * redirect, which is particularly disruptive in a long source list or news
 * feed. Keep one short-lived position for the current page and restore it
 * only after a successful navigation back to that same page.
 */
(() => {
  const storageKey = "newsrsshub:pending-scroll-restore";
  const refreshStorageKey = "newsrsshub:pending-return-refresh";
  const maxAgeMs = 30_000;
  const refreshMaxAgeMs = 5 * 60_000;

  const markReturnForRefresh = (form) => {
    const target = form.closest("[data-refresh-return]")?.dataset.refreshReturn;
    if (!target) return;

    try {
      const targetUrl = new URL(target, window.location.origin);
      sessionStorage.setItem(
        refreshStorageKey,
        JSON.stringify({ pathname: targetUrl.pathname, createdAt: Date.now() }),
      );
    } catch {
      // 存储不可用时仍按正常 POST + 跳转流程完成收藏操作。
    }
  };

  const refreshReturnedList = () => {
    try {
      const stored = sessionStorage.getItem(refreshStorageKey);
      if (!stored) return;

      const pending = JSON.parse(stored);
      if (
        !pending.pathname ||
        !Number.isFinite(pending.createdAt) ||
        Date.now() - pending.createdAt > refreshMaxAgeMs
      ) {
        sessionStorage.removeItem(refreshStorageKey);
        return;
      }
      if (pending.pathname !== window.location.pathname) return;

      // 先清除标记，再刷新从浏览器历史缓存恢复的列表，避免循环刷新。
      sessionStorage.removeItem(refreshStorageKey);
      window.location.reload();
    } catch {
      // 私密模式等受限环境可能连清理存储也失败，不能影响页面返回。
    }
  };

  const readPendingPosition = () => {
    try {
      const stored = sessionStorage.getItem(storageKey);
      if (!stored) return null;
      sessionStorage.removeItem(storageKey);

      const pending = JSON.parse(stored);
      if (
        pending.pathname !== window.location.pathname ||
        !Number.isFinite(pending.top) ||
        !Number.isFinite(pending.createdAt) ||
        Date.now() - pending.createdAt > maxAgeMs
      ) {
        return null;
      }
      return pending.top;
    } catch {
      return null;
    }
  };

  const restorePendingPosition = () => {
    const top = readPendingPosition();
    if (top === null) return;

    // The second frame waits for notice banners and the full list layout.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => window.scrollTo({ top, left: 0, behavior: "auto" }));
    });
  };

  const revealSelectedSourcePlatform = () => {
    const selected = document.querySelector(".source-platform-tab.selected");
    if (!selected) return;

    // 窄屏标签栏可以横向滚动；打开某个平台时把当前标签带进视野。
    selected.scrollIntoView({ block: "nearest", inline: "center", behavior: "auto" });
  };

  document.addEventListener("DOMContentLoaded", () => {
    restorePendingPosition();
    revealSelectedSourcePlatform();
  });

  // pageshow 同时覆盖正常跳转和从 BFCache 恢复的“返回热点列表”。
  window.addEventListener("pageshow", refreshReturnedList);

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (
        !(form instanceof HTMLFormElement) ||
        form.method.toLowerCase() !== "post" ||
        form.dataset.scrollRestore === "off"
      ) {
        return;
      }

      markReturnForRefresh(form);

      try {
        sessionStorage.setItem(
          storageKey,
          JSON.stringify({
            pathname: window.location.pathname,
            top: window.scrollY,
            createdAt: Date.now(),
          }),
        );
      } catch {
        // Storage may be unavailable in a private or constrained browser.
        // The form action remains fully functional in that case.
      }
    },
    true,
  );
})();
