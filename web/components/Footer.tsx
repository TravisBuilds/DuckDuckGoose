import MarketingOnly from "@/components/MarketingOnly";

export default function Footer() {
  return (
    <footer className="border-t border-gray-200 mt-auto">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex flex-col md:flex-row justify-between items-center gap-4">
          <p className="text-sm text-gray-600">
            © 2026 DuckDuckGoose. All rights reserved.
          </p>
          <MarketingOnly>
            <div className="flex gap-6 text-sm text-gray-600" data-testid="footer-coming-soon">
              <span>Terms (coming soon)</span>
              <span>Privacy (coming soon)</span>
              <span>Contact (coming soon)</span>
            </div>
          </MarketingOnly>
        </div>
      </div>
    </footer>
  );
}
