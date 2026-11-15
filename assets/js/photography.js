/* Photography section: world map, lightbox, gallery.
   Data: the JSON published by layouts/partials/photography/data-url.html,
   generated from data/photography.json (photography/build.py). */
(function () {
  'use strict';

  var app = document.getElementById('ph-app');
  if (!app) return;
  var cfg = app.dataset;
  var BASE = cfg.base.replace(/\/$/, '');
  var url = function (p) { return BASE + '/' + p; };

  // Sequential blue ramp: lighter = fewer photos, darker = more (all steps clear the dark land color)
  var BUCKETS = [
    { min: 1, color: '#b7d3f6' },
    { min: 5, color: '#86b6ef' },
    { min: 20, color: '#5598e7' },
    { min: 50, color: '#2a78d6' },
    { min: 100, color: '#1c5cab' }
  ];
  var ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';
  var TILES = {
    photos: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    countries: 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png'
  };
  if (cfg.cartoKey) {
    Object.keys(TILES).forEach(function (k) { TILES[k] += '?key=' + encodeURIComponent(cfg.cartoKey); });
  }
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // "2025-07-12T18:33:00" -> "12 Jul 2025, 18:33" (camera local time, no timezone shifts)
  function fmtDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || '');
    if (!m) return '';
    return (+m[3]) + ' ' + MONTHS[+m[2] - 1] + ' ' + m[1] + ', ' + m[4] + ':' + m[5];
  }

  function placeLine(p) {
    var parts = [];
    if (p.area && p.area !== p.city) parts.push(p.area);
    var cityCountry = [p.city, p.country].filter(Boolean).join(', ');
    if (cityCountry) parts.push(cityCountry);
    return parts.join(' · ');
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  // ------------------------------------------------------------------ lightbox

  function Lightbox(all, trips) {
    var el = document.getElementById('ph-lb');
    var img = el.querySelector('.ph-lb-img');
    var cap = el.querySelector('.ph-lb-cap');
    var count = el.querySelector('.ph-lb-count');
    var list = all, i = 0, lastFocus = null, touchX = null;

    function preload(k) {
      var p = list[(k + list.length) % list.length];
      if (p) { var im = new Image(); im.src = url(p.src); }
    }

    function show() {
      var p = list[i];
      img.classList.add('is-loading');
      img.onload = function () { img.classList.remove('is-loading'); };
      img.width = p.w; img.height = p.h;
      img.src = url(p.src);
      img.alt = p.caption || placeLine(p) || 'Photo';
      var title = p.caption || placeLine(p);
      var sub = [fmtDate(p.date)];
      if (p.caption && placeLine(p)) sub.unshift(placeLine(p));
      var t = trips[p.trip];
      if (t && cfg.mode === 'index') sub.push('<a href="' + cfg.trips + t.slug + '/">' + esc(t.title) + '</a>');
      var gear = [p.camera, p.lens, p.settings, p.film].filter(Boolean).map(esc).join(' · ');
      cap.innerHTML =
        (title ? '<p class="ph-lb-title">' + esc(title) + '</p>' : '') +
        '<p class="ph-lb-meta">' + sub.filter(Boolean).map(function (s) { return s.indexOf('<a ') === 0 ? s : esc(s); }).join(' · ') + '</p>' +
        (gear ? '<p class="ph-lb-gear">' + gear + '</p>' : '');
      count.textContent = list.length > 1 ? (i + 1) + ' / ' + list.length : '';
      el.classList.toggle('is-single', list.length < 2);
      try { history.replaceState(null, '', '#photo=' + encodeURIComponent(p.id)); } catch (e) { /* file:// */ }
      preload(i + 1); preload(i - 1);
    }

    function go(d) { if (list.length > 1) { i = (i + d + list.length) % list.length; show(); } }

    function open(id, subset) {
      list = subset && subset.length ? subset : all;
      i = list.findIndex(function (p) { return p.id === id; });
      if (i < 0) { list = all; i = all.findIndex(function (p) { return p.id === id; }); }
      if (i < 0) return;
      lastFocus = document.activeElement;
      el.hidden = false;
      document.documentElement.classList.add('ph-noscroll');
      show();
      el.querySelector('.ph-lb-close').focus();
    }

    function close() {
      el.hidden = true;
      img.removeAttribute('src');
      document.documentElement.classList.remove('ph-noscroll');
      try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* file:// */ }
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    el.querySelector('.ph-lb-close').addEventListener('click', close);
    el.querySelector('.ph-lb-prev').addEventListener('click', function () { go(-1); });
    el.querySelector('.ph-lb-next').addEventListener('click', function () { go(1); });
    el.addEventListener('click', function (e) { if (e.target === el || e.target.classList.contains('ph-lb-figure')) close(); });
    document.addEventListener('keydown', function (e) {
      if (el.hidden) return;
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowLeft') go(-1);
      else if (e.key === 'ArrowRight') go(1);
      else if (e.key === 'Tab') { // keep focus inside the dialog
        var f = Array.prototype.slice.call(el.querySelectorAll('button:not([hidden]), a[href]'))
          .filter(function (b) { return b.offsetParent !== null; });
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
        else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
      }
    });
    el.addEventListener('touchstart', function (e) { touchX = e.touches[0].clientX; }, { passive: true });
    el.addEventListener('touchend', function (e) {
      if (touchX == null) return;
      var dx = e.changedTouches[0].clientX - touchX;
      if (Math.abs(dx) > 50) go(dx < 0 ? 1 : -1);
      touchX = null;
    });

    return { open: open };
  }

  // ------------------------------------------------------------------ map

  function sameSpot(markers) {
    var a = markers[0].getLatLng();
    return markers.every(function (m) {
      var b = m.getLatLng();
      return Math.abs(a.lat - b.lat) < 1e-4 && Math.abs(a.lng - b.lng) < 1e-4;
    });
  }

  // Rings that cross the antimeridian jump from +180 to -180 and draw as horizontal
  // strips in Leaflet. Shift points by 360 degrees so each ring is continuous.
  function unwrapRing(ring) {
    for (var k = 1; k < ring.length; k++) {
      while (ring[k][0] - ring[k - 1][0] > 180) ring[k][0] -= 360;
      while (ring[k][0] - ring[k - 1][0] < -180) ring[k][0] += 360;
    }
  }
  function unwrapGeometry(g) {
    if (!g) return;
    if (g.type === 'Polygon') g.coordinates.forEach(unwrapRing);
    else if (g.type === 'MultiPolygon') g.coordinates.forEach(function (poly) { poly.forEach(unwrapRing); });
  }

  function pinIcon(p, n) {
    var badge = n > 1 ? '<span class="ph-pin-count">' + (n > 999 ? Math.round(n / 1000) + 'k' : n) + '</span>' : '';
    return L.divIcon({
      className: 'ph-pin' + (n > 1 ? ' ph-pin--cluster' : ''),
      html: '<img src="' + url(p.thumb) + '" alt="" loading="lazy" decoding="async">' + badge,
      iconSize: [48, 48],
      iconAnchor: [24, 24]
    });
  }

  function initMap(el, photos, lb) {
    var isIndex = cfg.mode === 'index';
    var located = photos.filter(function (p) { return p.lat != null && p.lon != null; });
    var map = L.map(el, {
      worldCopyJump: true,
      minZoom: 0,
      maxZoom: 17,
      zoomSnap: 0.25,
      scrollWheelZoom: false,
      attributionControl: true
    });
    map.attributionControl.setPrefix(false);
    // wheel zoom only after the visitor engages with the map, so the page still scrolls
    el.addEventListener('click', function () { map.scrollWheelZoom.enable(); });
    el.addEventListener('focus', function () { map.scrollWheelZoom.enable(); });
    el.addEventListener('mouseleave', function () { map.scrollWheelZoom.disable(); });

    var tiles = L.tileLayer(TILES[isIndex ? 'countries' : 'photos'], {
      attribution: ATTRIBUTION, subdomains: 'abcd', maxZoom: 19, detectRetina: true
    }).addTo(map);

    // photo pins, clustered; each cluster shows its first photo and a count
    var cluster = L.markerClusterGroup({
      showCoverageOnHover: false,
      zoomToBoundsOnClick: false,
      maxClusterRadius: 56,
      spiderfyDistanceMultiplier: 1.6,
      iconCreateFunction: function (c) {
        var ms = c.getAllChildMarkers();
        return pinIcon(ms[0].options.photo, ms.length);
      }
    });
    located.forEach(function (p) {
      var m = L.marker([p.lat, p.lon], { icon: pinIcon(p, 1), photo: p, keyboard: true, title: placeLine(p) || 'Photo' });
      m.on('click', function () { lb.open(p.id); });
      cluster.addLayer(m);
    });
    cluster.on('clusterclick', function (e) {
      var ms = e.layer.getAllChildMarkers();
      if (sameSpot(ms) || map.getZoom() >= map.getMaxZoom()) {
        // many photos tagged with the same spot (e.g. a trip fallback): browse them instead
        var subset = ms.map(function (m) { return m.options.photo; });
        lb.open(subset[0].id, subset);
      } else {
        e.layer.zoomToBounds({ padding: [48, 48] });
      }
    });

    if (!isIndex) {
      map.addLayer(cluster);
      if (located.length) {
        map.fitBounds(L.latLngBounds(located.map(function (p) { return [p.lat, p.lon]; })), { padding: [48, 48], maxZoom: 13 });
      } else {
        map.setView([20, 0], 2);
      }
      return;
    }

    // index page: countries (choropleth) and photos views
    // zoom so the whole world fits the map's width
    var fitZoom = Math.max(0, Math.floor(Math.log2(el.clientWidth / 256) * 4) / 4);
    map.setMinZoom(fitZoom);
    map.setView([30, 0], fitZoom);
    var counts = {}, names = {};
    photos.forEach(function (p) {
      if (p.cc) { counts[p.cc] = (counts[p.cc] || 0) + 1; names[p.cc] = p.country; }
    });
    var countryLayer = null;
    var legend = document.getElementById('ph-legend');
    var color = function (n) {
      var c = null;
      BUCKETS.forEach(function (b) { if (n >= b.min) c = b.color; });
      return c;
    };

    if (legend) {
      var max = Math.max.apply(null, Object.values(counts).concat([0]));
      var shown = BUCKETS.filter(function (b, k) { return k === 0 || b.min <= max; });
      legend.innerHTML = shown.map(function (b, k) {
        var next = shown[k + 1];
        var label = next ? (next.min - 1 === b.min ? b.min : b.min + '–' + (next.min - 1)) : b.min + '+';
        return '<span class="ph-legend-item"><span class="ph-legend-swatch" style="background:' + b.color + '"></span>' + label + '</span>';
      }).join('') + '<span class="ph-legend-unit">photos</span>';
    }

    // one setView (not fitBounds + setZoom) so the map redraws once, at the final view
    function zoomToPhotos(match, maxZoom, minZoom) {
      var pts = located.filter(match).map(function (p) { return [p.lat, p.lon]; });
      if (!pts.length) return false;
      var b = L.latLngBounds(pts);
      var z = Math.min(maxZoom, map.getBoundsZoom(b.pad(0.25)));
      map.setView(b.getCenter(), Math.max(z, minZoom || 0));
      return true;
    }

    // ---- states: shown in the countries view once zoomed in on a visited country
    var STATE_ZOOM = 3.5;
    var stateCounts = {}, stateLayers = {}, statesOn = false;
    photos.forEach(function (p) {
      if (p.cc && p.state) { var k = p.cc + '|' + p.state; stateCounts[k] = (stateCounts[k] || 0) + 1; }
    });

    function loadStates(cc) {
      if (stateLayers[cc] !== undefined) return Promise.resolve(stateLayers[cc]);
      return fetch(cfg.states + cc + '.json').then(function (r) { return r.ok ? r.json() : null; }).then(function (topo) {
        if (!topo) return (stateLayers[cc] = null);
        var geo = topojson.feature(topo, topo.objects[Object.keys(topo.objects)[0]]);
        geo.features.forEach(function (f) { unwrapGeometry(f.geometry); });
        var layer = L.geoJSON(geo, {
          style: function (f) {
            var n = stateCounts[cc + '|' + f.properties.n] || 0;
            return n
              ? { fillColor: color(n), fillOpacity: 0.9, color: '#0d1117', weight: 0.8 }
              : { fillColor: '#21262d', fillOpacity: 0.55, color: '#30363d', weight: 0.6 };
          },
          onEachFeature: function (f, sl) {
            var n = stateCounts[cc + '|' + f.properties.n] || 0;
            if (!n) { sl.options.interactive = false; return; }
            sl.bindTooltip('<strong>' + esc(f.properties.n) + '</strong>, ' + esc(names[cc] || cc) + '<br>' + plural(n, 'photo'),
              { sticky: true, className: 'ph-tip', direction: 'top', offset: [0, -8] });
            sl.on('mouseover', function () { sl.setStyle({ weight: 2, color: '#e6edf3' }); sl.bringToFront(); });
            sl.on('mouseout', function () { layer.resetStyle(sl); });
            sl.on('click', function () {
              setView('photos');
              zoomToPhotos(function (p) { return p.cc === cc && p.state === f.properties.n; }, 11);
            });
          }
        });
        return (stateLayers[cc] = layer);
      }).catch(function () { return (stateLayers[cc] = null); });
    }

    function countryStyle(f) {
      var n = counts[f.properties.a2] || 0;
      if (n && statesOn) return { fillOpacity: 0, color: '#484f58', weight: 1 }; // its states draw the fill
      return n
        ? { fillColor: color(n), fillOpacity: 0.9, color: '#0d1117', weight: 0.8 }
        : { fillColor: '#21262d', fillOpacity: 0.55, color: '#30363d', weight: 0.5 };
    }

    function updateStates() {
      var want = el.dataset.view === 'countries' && map.getZoom() >= STATE_ZOOM;
      if (want === statesOn) return;
      statesOn = want;
      if (countryLayer) countryLayer.setStyle(countryStyle);
      Object.keys(counts).forEach(function (cc) {
        if (want) {
          loadStates(cc).then(function (l) { if (l && statesOn) l.addTo(map); });
        } else if (stateLayers[cc]) {
          map.removeLayer(stateLayers[cc]);
        }
      });
    }
    map.on('zoomend', updateStates);

    fetch(cfg.geo).then(function (r) { return r.json(); }).then(function (topo) {
      var geo = topojson.feature(topo, topo.objects.countries);
      geo.features = geo.features.filter(function (f) { return f.properties.a2 !== 'AQ'; });
      geo.features.forEach(function (f) { unwrapGeometry(f.geometry); });
      countryLayer = L.geoJSON(geo, {
        style: countryStyle,
        onEachFeature: function (f, layer) {
          var n = counts[f.properties.a2] || 0;
          if (!n) { layer.options.interactive = false; return; }
          layer.bindTooltip('<strong>' + esc(names[f.properties.a2] || f.properties.name) + '</strong><br>' + plural(n, 'photo'), { sticky: true, className: 'ph-tip', direction: 'top', offset: [0, -8] });
          layer.on('mouseover', function () { if (!statesOn) { layer.setStyle({ weight: 2, color: '#e6edf3' }); layer.bringToFront(); } });
          layer.on('mouseout', function () { layer.setStyle(countryStyle(f)); });
          layer.on('click', function () {
            // drill down: zoom to this country's photos, where its states appear
            if (!zoomToPhotos(function (p) { return p.cc === f.properties.a2; }, 6, STATE_ZOOM)) {
              map.setView(layer.getBounds().getCenter(), STATE_ZOOM);
            }
          });
        }
      });
      if (el.dataset.view === 'countries') { countryLayer.addTo(map); countryLayer.bringToBack(); }
    }).catch(function () { /* map still works with photos */ });

    var buttons = document.querySelectorAll('.ph-seg-btn');
    function setView(view) {
      el.dataset.view = view;
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.view === view)); });
      tiles.setUrl(TILES[view]);
      if (legend) legend.hidden = view !== 'countries';
      if (view === 'countries') {
        map.removeLayer(cluster);
        if (countryLayer) { countryLayer.addTo(map); countryLayer.bringToBack(); }
      } else {
        if (countryLayer) map.removeLayer(countryLayer);
        map.addLayer(cluster);
      }
      updateStates();
    }
    buttons.forEach(function (b) { b.addEventListener('click', function () { setView(b.dataset.view); }); });
    setView(el.dataset.view || 'countries');
  }

  // ------------------------------------------------------------------ boot

  function boot() {
    fetch(cfg.json).then(function (r) { return r.json(); }).then(function (data) {
      var trips = {};
      data.trips.forEach(function (t) { trips[t.slug] = t; });
      var photos = cfg.mode === 'trip'
        ? data.photos.filter(function (p) { return p.trip === cfg.trip; })
        : data.photos;
      var lb = Lightbox(photos, trips);

      document.querySelectorAll('.ph-item[data-id]').forEach(function (a) {
        a.addEventListener('click', function (e) {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return; // let "open in new tab" work
          e.preventDefault();
          lb.open(a.dataset.id);
        });
      });

      var mapEl = document.getElementById('ph-map');
      if (mapEl && window.L) initMap(mapEl, photos, lb);

      var m = /^#photo=(.+)$/.exec(location.hash);
      if (m) lb.open(decodeURIComponent(m[1]));
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
