/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Same-origin topology is preferred (PRD §13.2) so the tracker's sendBeacon carries the auth
  // cookie without SameSite=None. When the backend runs separately in local dev, proxy the API to
  // FastAPI on :8000 so the browser only ever talks to the Next origin.
  async rewrites() {
    const backend = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/health", destination: `${backend}/health` },
    ];
  },
};

export default nextConfig;
