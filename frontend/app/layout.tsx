import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Unzoom — You deserve the truth.",
  description: "See the source, what changed, and the context left out of a post.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
