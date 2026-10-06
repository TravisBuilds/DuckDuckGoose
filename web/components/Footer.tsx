export default function Footer() {
  return (
    <footer className="border-t border-gray-200 dark:border-gray-800 mt-auto">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex flex-col md:flex-row justify-between items-center gap-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            © 2026 DuckDuckGoose. All rights reserved.
          </p>
          <div className="flex gap-6 text-sm text-gray-600 dark:text-gray-400">
            <span>Terms (coming soon)</span>
            <span>Privacy (coming soon)</span>
            <span>Contact (coming soon)</span>
          </div>
        </div>
      </div>
    </footer>
  );
}
