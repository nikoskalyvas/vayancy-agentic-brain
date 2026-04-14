/**
 * Vayancy Extension Phase 2 — background/ranking-tracker.js
 *
 * Automatically tracks a property's real ranking position on Booking.com
 * by performing guest-facing searches and parsing the results.
 *
 * Why this matters:
 *   Official BDC APIs tell you your rate was updated. They never tell you
 *   that a visibility policy violation pushed you to page 5.
 *   This is the "black box" data — extracted exactly as a guest would see it.
 *
 * Architecture:
 *   - Runs as a background fetch (not in the browser tab, no UI needed)
 *   - Uses BDC's public search endpoint with structured params
 *   - Parses JSON search response to find property rank position
 *   - Rate-limited: max 3 searches per property per day (avoids detection)
 *   - Results stored via service worker → backend /extension/snapshots
 *
 * Trigger: Called from service-worker.js on the RANK_CHECK alarm (daily)
 * or manually via sendMessage({ type: "CHECK_RANKING_NOW" }).
 */

const RANK_ALARM       = "vayancy_rank_check";
const RANK_INTERVAL    = 1440;  // minutes = 24h
const MAX_DAILY_CHECKS = 3;
const MIN_WAIT_BETWEEN = 4 * 60 * 60 * 1000;  // 4h in ms

// BDC search config
const BDC_SEARCH_BASE  = "https://www.booking.com/searchresults.en-gb.html";
const BDC_API_BASE     = "https://www.booking.com/dml/graphql";
const DEFAULT_CHECKIN_OFFSET  = 14;  // days from today
const DEFAULT_CHECKOUT_OFFSET = 17;  // 3-night search

// ── Daily alarm setup ─────────────────────────────────────────────────────────

export function setupRankingAlarm() {
  chrome.alarms.get(RANK_ALARM, (alarm) => {
    if (!alarm) {
      chrome.alarms.create(RANK_ALARM, {
        periodInMinutes: RANK_INTERVAL,
        delayInMinutes:  60,  // first check 1h after install
      });
    }
  });
}

// ── Main tracking function ────────────────────────────────────────────────────

export async function checkPropertyRanking(state) {
  const pid   = state?.current_property_id;
  const prop  = pid ? state?.properties?.[pid] : null;
  const cfg   = state?.config || {};

  if (!pid || !prop) {
    console.log("[Vayancy Ranking] No active property — skipping");
    return null;
  }

  // Rate limit: max 3 checks per day
  const today     = new Date().toISOString().split("T")[0];
  const rankMeta  = state?.ranking_meta?.[pid] || {};
  const todayCount = rankMeta.date === today ? (rankMeta.count || 0) : 0;

  if (todayCount >= MAX_DAILY_CHECKS) {
    console.log(`[Vayancy Ranking] Daily limit reached (${todayCount}/${MAX_DAILY_CHECKS})`);
    return null;
  }

  // Minimum gap between checks
  const lastCheck = rankMeta.last_check_ms || 0;
  if (Date.now() - lastCheck < MIN_WAIT_BETWEEN) {
    console.log("[Vayancy Ranking] Too soon since last check — skipping");
    return null;
  }

  const location = prop.location || prop.property?.location || "";
  if (!location) {
    console.log("[Vayancy Ranking] No location for property — skipping");
    return null;
  }

  console.log(`[Vayancy Ranking] Checking rank for property ${pid} in ${location}`);

  try {
    const result = await fetchRankingData(pid, prop, location);

    if (result) {
      // Update rate-limiting meta
      await updateRankMeta(pid, today, todayCount + 1);

      // Push to service worker for storage + sync
      return {
        type:        "XHR_INTERCEPTED",
        category:    "ranking",
        property_id: pid,
        timestamp:   new Date().toISOString(),
        data:        result,
        source:      "ranking_tracker",
      };
    }
  } catch (e) {
    console.error("[Vayancy Ranking] Check failed:", e.message);
  }

  return null;
}

// ── BDC Search fetch ──────────────────────────────────────────────────────────

async function fetchRankingData(propertyId, prop, location) {
  const { checkin, checkout } = getSearchDates();

  // Randomise params slightly to appear organic
  const adults = [2, 3, 4][Math.floor(Math.random() * 3)];

  // BDC search URL that returns JSON when Accept: application/json
  const params = new URLSearchParams({
    ss:           location,
    checkin_year:  checkin.year,
    checkin_month: checkin.month,
    checkin_mday:  checkin.day,
    checkout_year:  checkout.year,
    checkout_month: checkout.month,
    checkout_mday:  checkout.day,
    group_adults:   adults,
    no_rooms:       1,
    order:          "popularity",
    offset:         0,
    rows:           50,  // fetch enough to find our rank
  });

  // Add jitter delay (1-3s) before fetching — looks organic
  await sleep(1000 + Math.random() * 2000);

  const response = await fetch(`${BDC_SEARCH_BASE}?${params}`, {
    headers: {
      "Accept":          "application/json, text/html",
      "Accept-Language": "en-GB,en;q=0.9",
      "Cache-Control":   "no-cache",
    },
    credentials: "omit",  // no auth cookies — pure guest view
  });

  if (!response.ok) {
    throw new Error(`BDC search returned ${response.status}`);
  }

  const text = await response.text();

  // Try JSON parse first (BDC often returns structured data)
  let searchData = null;
  try {
    searchData = JSON.parse(text);
  } catch {
    // HTML response — parse as text
    searchData = parseSearchHtml(text, propertyId);
  }

  if (!searchData) return null;

  return extractRankFromResults(searchData, propertyId, prop, location, {
    checkin: `${checkin.year}-${String(checkin.month).padStart(2,'0')}-${String(checkin.day).padStart(2,'0')}`,
    checkout: `${checkout.year}-${String(checkout.month).padStart(2,'0')}-${String(checkout.day).padStart(2,'0')}`,
    adults,
  });
}

// ── Result parsing ────────────────────────────────────────────────────────────

function extractRankFromResults(data, propertyId, prop, location, searchParams) {
  // BDC returns results in different shapes depending on endpoint/version.
  // We check multiple known shapes.

  const results = (
    data?.results ||
    data?.search_results ||
    data?.properties ||
    data?.hotels ||
    []
  );

  if (!Array.isArray(results) || results.length === 0) {
    return null;
  }

  const totalResults = results.length;

  // Find our property by ID or name match
  let rankPosition  = null;
  let ourData       = null;
  let competitorADR = [];

  for (let i = 0; i < results.length; i++) {
    const r = results[i];
    const rid   = String(r.hotel_id || r.id || r.property_id || "");
    const rname = (r.hotel_name || r.name || r.property_name || "").toLowerCase();
    const ours  = (prop.name || "").toLowerCase();

    // Match by property_id from Extranet (stored in prop during browsing)
    const extranetId = String(prop.extranet_id || prop.property_id || "");

    if ((extranetId && rid === extranetId) ||
        (ours && rname.includes(ours.split(" ")[0]))) {
      rankPosition = i + 1;
      ourData      = r;
    }

    // Collect competitor ADR for benchmarking
    const price = r.min_total_price || r.price || r.min_price;
    if (price && typeof price === "number" && price > 0) {
      competitorADR.push(price);
    }
  }

  const medianCompetitorPrice = competitorADR.length > 0
    ? Math.round(median(competitorADR))
    : null;

  const ourPublicPrice = ourData
    ? (ourData.min_total_price || ourData.price || null)
    : null;

  return {
    // Rank data
    rank_position:          rankPosition,
    rank_total:             totalResults,
    rank_percentile:        rankPosition
                              ? Math.round(((totalResults - rankPosition) / totalResults) * 100)
                              : null,

    // Price benchmarking
    our_public_price:       ourPublicPrice,
    median_competitor_price: medianCompetitorPrice,
    price_vs_median_pct:    (ourPublicPrice && medianCompetitorPrice)
                              ? Math.round(((ourPublicPrice - medianCompetitorPrice) / medianCompetitorPrice) * 100)
                              : null,

    // Search context
    search_location:   location,
    search_checkin:    searchParams.checkin,
    search_checkout:   searchParams.checkout,
    search_adults:     searchParams.adults,
    competitor_count:  totalResults,

    // Source
    data_source:  "public_search",
    captured_at:  new Date().toISOString(),
  };
}

function parseSearchHtml(html, propertyId) {
  // Fallback: extract JSON-LD or embedded JS data from HTML response.
  // BDC often embeds search results as window.b_search_results = {...}
  const match = html.match(/window\.b_search_results\s*=\s*(\{.+?\});/s) ||
                html.match(/data-results="([^"]+)"/);
  if (match) {
    try {
      return JSON.parse(match[1].replace(/&quot;/g, '"'));
    } catch {
      return null;
    }
  }
  return null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function getSearchDates() {
  const base = new Date();
  base.setDate(base.getDate() + DEFAULT_CHECKIN_OFFSET);
  const ci = { year: base.getFullYear(), month: base.getMonth() + 1, day: base.getDate() };
  base.setDate(base.getDate() + (DEFAULT_CHECKOUT_OFFSET - DEFAULT_CHECKIN_OFFSET));
  const co = { year: base.getFullYear(), month: base.getMonth() + 1, day: base.getDate() };
  return { checkin: ci, checkout: co };
}

function median(arr) {
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 !== 0
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function updateRankMeta(propertyId, today, newCount) {
  const STORAGE_KEY = "vayancy_state";
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const state = data[STORAGE_KEY] || {};
  if (!state.ranking_meta) state.ranking_meta = {};
  state.ranking_meta[propertyId] = {
    date:          today,
    count:         newCount,
    last_check_ms: Date.now(),
  };
  await chrome.storage.local.set({ [STORAGE_KEY]: state });
}
