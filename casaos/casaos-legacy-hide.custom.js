/* NAS_DEMO_LEGACY_HIDE_BEGIN */
(function () {
  // 容器型条目（app_type=container）的 title.en_us 就是容器名，按容器名匹配。
  // 注意：不能把 'openclaw' 用于应用型条目，否则会误伤显示名为 "OpenClaw" 的入口卡片。
  var hideContainerTitles = {
    "openclaw": true,
    "knowledge_base": true,
    "media_downloader": true,
    "immich-machine-learning": true,
    "immich-postgres": true,
    "immich-redis": true
  };

  // 应用型条目（app_type=v2app）的 name 是应用 ID / compose 项目名，按 ID 匹配。
  // ai-nas / demo-smart-nas-system / nas-demo 均为「非 CasaOS 注册来源」的 compose 项目留下的幽灵卡片。
  var hideAppNames = {
    "ai-nas": true,
    "demo-smart-nas-system": true,
    "nas-demo": true
  };

  // 幽灵条目：CasaOS 会把非注册来源的 compose 项目也列进 appgrid，
  // 这类条目缺少 status / port / store_app_id 等真实应用字段，点开是空页面。
  // 正常应用即使停止运行也仍带 port 与 store_app_id，不会被误伤。
  function isGhostApp(item) {
    var hasStatus = String(item.status || "").length > 0;
    var hasPort = String(item.port || "").length > 0;
    var hasStoreId = String(item.store_app_id || "").length > 0;
    return !hasStatus && !hasPort && !hasStoreId;
  }

  function normalized(item, key) {
    return String((item && item[key]) || "").trim().toLowerCase();
  }

  function itemTitle(item) {
    var t = (item && item.title) || {};
    return String(t.en_us || t.en_US || "").trim().toLowerCase();
  }

  function shouldHideLegacyItem(item) {
    if (!item) {
      return false;
    }
    if (item.app_type === "container") {
      return !!hideContainerTitles[itemTitle(item)];
    }
    if (item.app_type === "v2app") {
      return !!hideAppNames[normalized(item, "name")] || isGhostApp(item);
    }
    return false;
  }

  function filterAppGridPayload(payload) {
    if (!payload || !Array.isArray(payload.data)) {
      return payload;
    }
    payload.data = payload.data.filter(function (item) {
      return !shouldHideLegacyItem(item);
    });
    return payload;
  }

  function isAppGridUrl(url) {
    return String(url || "").indexOf("/v2/app_management/web/appgrid") !== -1;
  }

  var rawFetch = window.fetch;
  if (typeof rawFetch === "function") {
    window.fetch = function (input, init) {
      return rawFetch(input, init).then(function (resp) {
        try {
          var url = typeof input === "string" ? input : (input && input.url) || "";
          if (!isAppGridUrl(url)) {
            return resp;
          }
          return resp.clone().json().then(function (obj) {
            var filtered = filterAppGridPayload(obj);
            return new Response(JSON.stringify(filtered), {
              status: resp.status,
              statusText: resp.statusText,
              headers: resp.headers
            });
          }).catch(function () {
            return resp;
          });
        } catch (_err) {
          return resp;
        }
      });
    };
  }

  var rawOpen = XMLHttpRequest.prototype.open;
  var rawSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__nasDemoUrl = url || "";
    return rawOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function () {
    var xhr = this;
    xhr.addEventListener("readystatechange", function () {
      try {
        if (xhr.readyState !== 4 || xhr.status !== 200 || !isAppGridUrl(xhr.__nasDemoUrl)) {
          return;
        }
        var raw = xhr.responseText;
        if (!raw) {
          return;
        }
        var parsed = JSON.parse(raw);
        var patched = JSON.stringify(filterAppGridPayload(parsed));
        Object.defineProperty(xhr, "responseText", {
          configurable: true,
          get: function () {
            return patched;
          }
        });
        Object.defineProperty(xhr, "response", {
          configurable: true,
          get: function () {
            return patched;
          }
        });
      } catch (_err) {
        return;
      }
    });
    return rawSend.apply(this, arguments);
  };
})();
/* NAS_DEMO_LEGACY_HIDE_END */
