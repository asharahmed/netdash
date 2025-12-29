export async function fetchStatus() {
  const res = await fetch("/api/status", { cache: "no-store" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Status fetch failed (${res.status}): ${text || res.statusText}`);
  }
  return res.json();
}
