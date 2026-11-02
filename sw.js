/*
  АкБарак — service worker.
  Делает одно: принимает файлы из меню «Поделиться» Android (WhatsApp, Telegram, «Файлы»…),
  когда сайт установлен как приложение. Файлы кладём во временный кэш и открываем
  страницу печати с ними. Остальные запросы не трогаем — сайт работает как обычно.
*/
const SHARE_CACHE = 'akbarak-share';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'POST' || url.pathname !== '/share-target') return;

  event.respondWith((async () => {
    try {
      const form = await event.request.formData();
      const files = form.getAll('file').filter(f => f && typeof f === 'object' && f.size > 0);
      const cache = await caches.open(SHARE_CACHE);
      for (const key of await cache.keys()) await cache.delete(key);
      const list = [];
      for (let i = 0; i < files.length && i < 10; i++) {
        const key = '/shared/' + i;
        await cache.put(key, new Response(files[i], {
          headers: {
            'content-type': files[i].type || 'application/octet-stream',
            'x-file-name': encodeURIComponent(files[i].name || ('файл-' + (i + 1))),
          },
        }));
        list.push(key);
      }
      await cache.put('/shared/list', new Response(JSON.stringify(list), {headers: {'content-type': 'application/json'}}));
      return Response.redirect('/?shared=1', 303);
    } catch (e) {
      return Response.redirect('/?shared=error', 303);
    }
  })());
});
