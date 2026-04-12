import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Vayancy — Owner Dashboard",
  description: "Agentic Brain for Luxury Hospitality",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="min-h-screen bg-vayancy-bg text-vayancy-text">
        {children}
      </body>
    </html>
  );
}
