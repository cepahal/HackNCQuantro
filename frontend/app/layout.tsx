import type { Metadata } from "next";
import Navbar from "../components/Navbar";
import "./globals.css";

export const metadata: Metadata = { title: "Quantro — Check the context", description: "Compare a public post with source evidence." };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><Navbar/><main>{children}</main><footer className="site-footer">Quantro · Sources, context, and clearly stated uncertainty</footer></body></html>;
}
