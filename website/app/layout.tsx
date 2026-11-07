import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://pliqra.etlabio.com"),
  title: "Pliqra — Self-hosted AI gateways. Explicit model contracts.",
  description: "Turn model endpoints into governed AI services. Custom gateways, guardrails, usage visibility, active/active sites and reviewed fleet policies. One Docker image to start.",
  alternates: {canonical: "/"},
  openGraph: {title: "Pliqra — Your models. Your rules. One clear contract.", description: "A self-hosted AI gateway with explicit model contracts, composable guardrails, usage visibility and multi-DC governance.", type: "website", url: "/", siteName: "Pliqra", images: [{url: "/og.png", width: 1200, height: 630, alt: "Pliqra — self-hosted AI services with explicit model contracts"}]},
  twitter: {card: "summary_large_image", title: "Pliqra — Your models. Your rules. One clear contract.", description: "Bring a model endpoint. Publish a governed AI service. Self-hosted, platform-independent and Apache-2.0.", images: ["/og.png"]},
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
      <body className="antialiased"><script type="application/ld+json" dangerouslySetInnerHTML={{__html:JSON.stringify({"@context":"https://schema.org","@type":"SoftwareApplication",name:"Pliqra",url:"https://pliqra.etlabio.com",description:"Self-hosted AI services with explicit model contracts, custom gateways, guardrails, usage visibility and multi-DC governance.",applicationCategory:"DeveloperApplication",operatingSystem:"Linux containers: AMD64 and ARM64",softwareVersion:"2.1.0",image:"https://pliqra.etlabio.com/og.png",license:"https://github.com/eturkok/pliqra/blob/main/LICENSE",offers:{"@type":"Offer",price:"0",priceCurrency:"USD"}}).replace(/</g,"\\u003c")}}/>{children}</body>
    </html>
  );
}
