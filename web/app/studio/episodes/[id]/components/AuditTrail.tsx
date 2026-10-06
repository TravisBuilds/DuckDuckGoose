'use client';

import { useEffect, useState } from 'react';

interface AuditEvent {
  id: number;
  event_type: string;
  event_data: any;
  created_at: string;
}

interface AuditTrailProps {
  episodeId: string;
}

export default function AuditTrail({ episodeId }: AuditTrailProps) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadEvents();
    
    const interval = setInterval(loadEvents, 10000);
    return () => clearInterval(interval);
  }, [episodeId]);

  async function loadEvents() {
    try {
      const response = await fetch(`/api/studio/episodes/${episodeId}/audit`);
      if (response.ok) {
        const data = await response.json();
        setEvents(data);
      }
    } catch (error) {
      console.error('Failed to load audit trail:', error);
    } finally {
      setLoading(false);
    }
  }

  function formatEventType(type: string): string {
    return type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
  }

  if (loading) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-gray-500">Loading audit trail...</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">
        Audit Trail
      </h3>
      
      {events.length === 0 ? (
        <p className="text-gray-500 text-sm">No events yet</p>
      ) : (
        <div className="space-y-3">
          {events.map((event) => (
            <div
              key={event.id}
              className="border-l-4 border-blue-500 bg-gray-50 p-3 rounded-r"
            >
              <div className="flex items-start justify-between mb-1">
                <span className="text-sm font-medium text-gray-900">
                  {formatEventType(event.event_type)}
                </span>
                <span className="text-xs text-gray-500">
                  {new Date(event.created_at).toLocaleString()}
                </span>
              </div>
              
              {Object.keys(event.event_data).length > 0 && (
                <pre className="text-xs text-gray-600 mt-2 overflow-x-auto">
                  {JSON.stringify(event.event_data, null, 2)}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
