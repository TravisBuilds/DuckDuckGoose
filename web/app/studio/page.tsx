export default function StudioHomePage() {
  return (
    <div className="max-w-7xl mx-auto py-8 px-4">
      <h1 className="text-3xl font-bold text-gray-900 mb-4">
        DuckDuckGoose Studio Console
      </h1>
      
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-gray-600 mb-4">
          Production console for DuckDuckGoose episodes. Video workflow up to picture lock (G4.09).
        </p>
        
        <div className="flex gap-4">
          <a
            href="/studio/episodes/ep04"
            className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700 transition"
          >
            Open Episode 04
          </a>
        </div>
      </div>
    </div>
  );
}
