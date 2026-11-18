import Link from 'next/link';
import { HOME_COPY } from '@/lib/home-copy.mjs';
import { STUDIO_HREF } from './links';
import { EarlyAccessLink, Wordmark } from './parts';
import styles from './home.module.css';

export default function HomeHeader() {
  const copy = HOME_COPY.header;
  return (
    <header className={styles.header}>
      <a href="#main" className={styles.skip}>{copy.skip}</a>
      <div className={styles.headerBar}>
        <Link href="/" aria-current="page" className={styles.wordmark}>
          <Wordmark />
        </Link>
        <nav aria-label={copy.nav} className={styles.headerNav}>
          <Link href={STUDIO_HREF} prefetch={false} className={styles.signIn}>{copy.signIn}</Link>
          <EarlyAccessLink label={copy.cta} size="small" className={styles.headerCta} />
        </nav>
      </div>
    </header>
  );
}
