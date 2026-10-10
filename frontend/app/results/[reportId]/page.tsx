import PhotonBanner from "../../../components/PhotonBanner";
import ReportPage from "../../../components/ReportPage";
import ShareButton from "../../../components/ShareButton";

export default async function Results({ params }: { params: Promise<{ reportId: string }> }) {
  const { reportId } = await params;
  return <><ShareButton reportId={reportId}/><PhotonBanner/><ReportPage reportId={reportId}/></>;
}
