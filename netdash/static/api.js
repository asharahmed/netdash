export async function fetchStatus(options = {}) {
  const params = new URLSearchParams();
  if (options.force) params.set("fresh", "1");
  const url = `/api/status${params.toString() ? `?${params.toString()}` : ""}`;
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 12000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(url, {
      cache: "no-store",
      headers: {
        "Cache-Control": "no-store",
        Pragma: "no-cache"
      },
      signal: controller.signal
    });
  } catch (err) {
    if (err && (err.name === "AbortError" || String(err.message).toLowerCase().includes("aborted"))) {
      throw new Error("Status fetch timed out");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Status fetch failed (${res.status}): ${text || res.statusText}`);
  }
  return res.json();
}
