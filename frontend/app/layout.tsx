import type { Metadata } from "next";
import "./globals.css";
import PointerCursor from "../components/PointerCursor";

export const metadata: Metadata = {
  title: "Zoomout — Political speeches in context.",
  description: "Compare political debate and speech excerpts with their full recordings and surrounding context.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}<PointerCursor/></body></html>;
}
