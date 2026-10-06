'use client';

import { useState } from 'react';

interface EpisodeUploadProps {
  episodeId: string;
  onUploadComplete?: () => void;
}

export default function EpisodeUpload({ episodeId, onUploadComplete }: EpisodeUploadProps) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleUpload(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setUploading(true);
    setError(null);
    setSuccess(false);

    const formData = new FormData(e.currentTarget);

    try {
      const response = await fetch(`/api/studio/episodes/${episodeId}/upload`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        throw new Error('Upload failed');
      }

      const result = await response.json();
      setSuccess(true);
      
      if (onUploadComplete) {
        onUploadComplete();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">
        Upload Episode Data
      </h3>
      
      <form onSubmit={handleUpload} className="space-y-4">
        <div>
          <label htmlFor="beatmap" className="block text-sm font-medium text-gray-700 mb-1">
            BEATMAP.md (required)
          </label>
          <input
            type="file"
            id="beatmap"
            name="beatmap"
            accept=".md"
            required
            className="block w-full text-sm text-gray-500
              file:mr-4 file:py-2 file:px-4
              file:rounded-md file:border-0
              file:text-sm file:font-semibold
              file:bg-blue-50 file:text-blue-700
              hover:file:bg-blue-100"
          />
        </div>

        <div>
          <label htmlFor="continuity" className="block text-sm font-medium text-gray-700 mb-1">
            CONTINUITY.md (optional)
          </label>
          <input
            type="file"
            id="continuity"
            name="continuity"
            accept=".md"
            className="block w-full text-sm text-gray-500
              file:mr-4 file:py-2 file:px-4
              file:rounded-md file:border-0
              file:text-sm file:font-semibold
              file:bg-blue-50 file:text-blue-700
              hover:file:bg-blue-100"
          />
        </div>

        <div>
          <label htmlFor="credit_plan" className="block text-sm font-medium text-gray-700 mb-1">
            CREDIT-PLAN.md (optional)
          </label>
          <input
            type="file"
            id="credit_plan"
            name="credit_plan"
            accept=".md"
            className="block w-full text-sm text-gray-500
              file:mr-4 file:py-2 file:px-4
              file:rounded-md file:border-0
              file:text-sm file:font-semibold
              file:bg-blue-50 file:text-blue-700
              hover:file:bg-blue-100"
          />
        </div>

        {error && (
          <div className="p-3 bg-red-50 border border-red-200 rounded text-sm text-red-800">
            {error}
          </div>
        )}

        {success && (
          <div className="p-3 bg-green-50 border border-green-200 rounded text-sm text-green-800">
            Files uploaded successfully
          </div>
        )}

        <button
          type="submit"
          disabled={uploading}
          className="w-full bg-blue-600 text-white py-2 px-4 rounded-md hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition"
        >
          {uploading ? 'Uploading...' : 'Upload Episode Data'}
        </button>
      </form>

      <p className="mt-4 text-xs text-gray-500">
        Files are stored backend-side and never committed to the public repository.
        Upload BEATMAP.md to parse shots and enable production workflow.
      </p>
    </div>
  );
}
