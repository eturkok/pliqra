import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://pliqra.etlabio.com"),
  title: "Pliqra — Self-hosted AI gateways. Explicit model contracts.",
  description: "Make model changes explicit, review policies across independent installations and gate releases on fresh evidence. Custom gateways, guardrails and usage visibility included.",
  alternates: {canonical: "/"},
  openGraph: {title: "Pliqra — Your models. Your rules. One clear contract.", description: "Explicit application contracts, reviewed fleet policies and evidence-gated model releases. Routing, guardrails and usage visibility in one self-hosted product.", type: "website", url: "/", siteName: "Pliqra", images: [{url: "/og.png", width: 1200, height: 630, alt: "Pliqra — self-hosted AI services with explicit model contracts"}]},
  twitter: {card: "summary_large_image", title: "Pliqra — Your models. Your rules. One clear contract.", description: "Explicit application contracts, reviewed fleet changes and evidence-gated releases. Self-hosted and Apache-2.0.", images: ["/og.png"]},
  robots: {index: true, follow: true},
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased"><script type="application/ld+json" dangerouslySetInnerHTML={{__html:JSON.stringify({"@context":"https://schema.org","@type":"SoftwareApplication",name:"Pliqra",url:"https://pliqra.etlabio.com",description:"Self-hosted AI services with explicit application contracts, reviewed fleet policies and evidence-gated releases, plus routing, guardrails and usage visibility.",applicationCategory:"DeveloperApplication",operatingSystem:"Linux containers: AMD64 and ARM64",softwareVersion:"2.1.0",image:"https://pliqra.etlabio.com/og.png",license:"https://github.com/eturkok/pliqra/blob/main/LICENSE",offers:{"@type":"Offer",price:"0",priceCurrency:"USD"}}).replace(/</g,"\\u003c")}}/>{children}</body>
    </html>
  );
}
