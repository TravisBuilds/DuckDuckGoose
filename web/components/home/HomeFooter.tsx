import Link from 'next/link';
import { HOME_COPY } from '@/lib/home-copy.mjs';
import { STUDIO_HREF } from './links';
import { Wordmark } from './parts';
import styles from './home.module.css';

/** The home page's own footer: the shared one (components/Footer.tsx) is left out here, see SiteChrome. */
export default function HomeFooter() {
  const copy = HOME_COPY.footer;
  return (
    <footer className={styles.footer}>
      <div className={`${styles.container} ${styles.footerInner}`}>
        <p className={styles.footerBrand}>
          <Wordmark />
        </p>
        <div className={styles.footerMeta}>
          <span>{copy.copyright}</span>
          <span data-testid="footer-coming-soon" className={styles.footerSoon}>
            {copy.comingSoon.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </span>
          <Link href={STUDIO_HREF} prefetch={false} className={styles.footerLink}>{copy.signIn}</Link>
        </div>
      </div>
    </footer>
  );
}
