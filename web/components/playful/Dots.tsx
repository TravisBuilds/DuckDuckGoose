/** Three dots in azure, lime and orange-red: a quiet accent above a short message, never a picture of anything. */
export default function Dots({ className = '' }: { className?: string }) {
  return (
    <span aria-hidden="true" className={`inline-flex items-end gap-1.5 ${className}`}>
      <span className="size-4 rounded-full bg-blue" />
      <span className="size-3 rounded-full bg-green" />
      <span className="size-2.5 rounded-full bg-coral" />
    </span>
  );
}
