const MAX_RETRIES = 3;
const RETRY_DELAYS = [500, 1500, 3000]; // Exponential backoff

async function fetchWithRetry(url, options, timeoutMs, onRetry) {
  let lastError = null;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const res = await fetch(url, {
        ...options,
        signal: controller.signal
      });
      clearTimeout(timer);

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Status fetch failed (${res.status}): ${text || res.statusText}`);
      }
      return res.json();

    } catch (err) {
      clearTimeout(timer);
      lastError = err;

      const isTimeout = err && (err.name === "AbortError" || String(err.message).toLowerCase().includes("aborted"));
      const isNetworkError = err && (err.name === "TypeError" || err.message.includes("fetch"));

      // Only retry on timeout or network errors, not on HTTP errors
      if ((isTimeout || isNetworkError) && attempt < MAX_RETRIES) {
        const delay = RETRY_DELAYS[attempt] || 3000;
        if (onRetry) onRetry(attempt + 1, MAX_RETRIES, delay);
        await new Promise(resolve => setTimeout(resolve, delay));
        continue;
      }

      if (isTimeout) {
        throw new Error("Status fetch timed out after retries");
      }
      throw err;
    }
  }

  throw lastError;
}

export async function fetchStatus(options = {}) {
  const params = new URLSearchParams();
  if (options.force) params.set("fresh", "1");
  const url = `/api/status${params.toString() ? `?${params.toString()}` : ""}`;
  const timeoutMs = options.timeoutMs ?? 12000;

  return fetchWithRetry(
    url,
    {
      cache: "no-store",
      headers: {
        "Cache-Control": "no-store",
        Pragma: "no-cache"
      }
    },
    timeoutMs,
    options.onRetry
  );
}
