/* NewsRSSHub 的 Service Worker 只负责系统通知，不缓存资讯页面。 */
self.addEventListener("push", (event) => {
  let payload = {
    title: "NewsRSSHub",
    body: "本轮抓取发现新内容，点此查看",
    url: "/",
    tag: "newsrsshub-fetch",
  };
  try {
    payload = { ...payload, ...(event.data ? event.data.json() : {}) };
  } catch (_error) {
    // Push 载荷异常时仍给出安全的通用提醒，不把原始错误显示给用户。
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/static/newsrsshub-icon.svg",
      badge: "/static/newsrsshub-icon.svg",
      tag: payload.tag || "newsrsshub-fetch",
      renotify: true,
      data: { url: payload.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (clients) => {
      const client = clients[0];
      if (client) {
        if ("navigate" in client) {
          await client.navigate(targetUrl);
        }
        return client.focus();
      }
      return self.clients.openWindow(targetUrl);
    }),
  );
});
