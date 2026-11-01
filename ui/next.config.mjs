/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // `next dev` only: hide the "N" dev-tools badge in the corner (build errors still show as an overlay).
  devIndicators: false,
  // `next dev` only: hosts (e.g. this laptop's LAN IP) allowed to load dev assets, for testing on a phone.
  allowedDevOrigins: (process.env.DEV_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean),
};

export default nextConfig;
