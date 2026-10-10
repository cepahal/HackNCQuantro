import type { Metadata } from "next";
import "./globals.css";
import PointerCursor from "../components/PointerCursor";

export const metadata: Metadata = {
  title: "Unzoom — You deserve the truth.",
  description: "See the source, what changed, and the context left out of a post.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}<PointerCursor/></body></html>;
}
