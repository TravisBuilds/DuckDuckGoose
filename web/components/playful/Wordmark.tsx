/** "DuckDuckGoose" in Montserrat with "Goose" in azure, as on the home page: the name in type, with no mark. */
export default function Wordmark({ className = '' }: { className?: string }) {
  return (
    <span className={`whitespace-nowrap font-display font-extrabold tracking-[-0.03em] text-navy ${className}`}>
      DuckDuck<span className="text-blue-deep">Goose</span>
    </span>
  );
}
