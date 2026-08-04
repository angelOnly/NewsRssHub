/*
 * 只在用户真正展开列表摘要时写入已读状态。请求不触发跳转，
 * 因此长列表的滚动位置和当前筛选条件都保持不变。
 */
(() => {
  const sentEventIds = new Set();

  const markAsRead = (eventId, preview) => {
    if (!eventId || sentEventIds.has(eventId)) return;

    sentEventIds.add(eventId);
    const card = preview.closest(".event-card");
    card?.classList.add("is-read");

    fetch(`/events/${encodeURIComponent(eventId)}/read`, {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => {
      // 网络暂时不可用时允许下次展开再次尝试，视觉状态保持本次阅读结果。
      sentEventIds.delete(eventId);
    });
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".event-preview[data-read-event-id]").forEach((preview) => {
      preview.addEventListener("toggle", () => {
        if (preview.open) markAsRead(preview.dataset.readEventId, preview);
      });
    });
  });
})();
