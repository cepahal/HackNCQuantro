export default function PhotonBanner() {
  const number = process.env.NEXT_PUBLIC_PHOTON_NUMBER?.trim();
  if (!number) return null;
  return <p className="notice">You can also send this URL to <strong>{number}</strong> on iMessage to verify it there.</p>;
}
