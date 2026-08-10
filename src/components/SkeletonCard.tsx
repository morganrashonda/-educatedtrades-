export function SkeletonCard({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton-card">
      <div className="skeleton-title mb-4" />
      <div className="space-y-3">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex justify-between">
            <div className="skeleton-text w-1/4" />
            <div className="skeleton-text w-1/3" />
          </div>
        ))}
      </div>
    </div>
  );
}

export function SkeletonTable({ rows = 5 }: { rows?: number }) {
  return (
    <div className="skeleton-card">
      <div className="skeleton-title mb-4" />
      <div className="space-y-4">
        {/* Header */}
        <div className="flex gap-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="skeleton-text w-16" />
          ))}
        </div>
        {/* Rows */}
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex gap-6">
            {Array.from({ length: 6 }).map((_, j) => (
              <div key={j} className="skeleton-text w-16" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function SkeletonChart() {
  return (
    <div className="skeleton-card h-[380px] flex items-center justify-center">
      <div className="space-y-4 w-full px-8">
        <div className="skeleton-title mb-6" />
        {/* Fake chart area */}
        <div className="relative h-[200px]">
          <div className="absolute inset-0 flex flex-col justify-between">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="skeleton-text w-full" />
            ))}
          </div>
          {/* Fake line */}
          <div className="absolute bottom-0 left-0 right-0 h-24 skeleton rounded-lg" style={{ clipPath: 'polygon(0 100%, 10% 80%, 20% 85%, 30% 60%, 40% 70%, 50% 40%, 60% 50%, 70% 30%, 80% 45%, 90% 20%, 100% 30%, 100% 100%)' }} />
        </div>
      </div>
    </div>
  );
}