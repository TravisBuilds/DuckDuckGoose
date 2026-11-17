import type { ReactNode } from 'react';

export interface PriceRow {
  label: ReactNode;
  value: ReactNode;
}

/**
 * The price shown before anything is spent (DESIGN-TARGETS-v2 §6.7): yellow-ringed white block,
 * the total in big type, the lines it is made of and what is left after. It only shows a price
 * the API gave; with none, the button it guards stays off.
 */
export default function PriceBlock({ total, status = 'Checked just now', rows = [], after, testId, children, className = '' }: {
  total: ReactNode;
  status?: ReactNode;
  rows?: PriceRow[];
  after?: PriceRow | null;
  testId?: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`pf-price ${className}`} data-testid={testId}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-extrabold">Price</span>
        {status && <span className="pf-chip pf-chip-warn">{status}</span>}
      </div>
      <p className="pf-price-big">{total}</p>
      {(rows.length > 0 || after) && (
        <table className="mt-2 w-full border-collapse text-[13.5px]">
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                <td className="py-1 pr-2">{row.label}</td>
                <td className="py-1 text-right font-extrabold">{row.value}</td>
              </tr>
            ))}
            {after && (
              <tr>
                <td className="border-t-2 border-dashed border-line pt-2 pr-2">{after.label}</td>
                <td className="border-t-2 border-dashed border-line pt-2 text-right font-extrabold">{after.value}</td>
              </tr>
            )}
          </tbody>
        </table>
      )}
      {children}
    </div>
  );
}
