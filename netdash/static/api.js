export async function fetchStatus(options = {}) {
  const params = new URLSearchParams();
  if (options.force) params.set("fresh", "1");
  const url = `/api/status${params.toString() ? `?${params.toString()}` : ""}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Status fetch failed (${res.status}): ${text || res.statusText}`);
  }
  return res.json();
}
