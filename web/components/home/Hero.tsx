import { HOME_COPY } from '@/lib/home-copy.mjs';
import { Blob, Dot, EarlyAccessLink } from './parts';
import styles from './home.module.css';

/** Slide 1: Your life, back. */
export default function Hero() {
  const copy = HOME_COPY.hero;
  return (
    <section aria-labelledby="home-hero-title" className={styles.hero}>
      <Blob shape="pebble" className={styles.heroLime} />
      <Blob shape="pebble" className={styles.heroCyan} />
      <span aria-hidden="true" className={styles.heroOval} />
      <Dot color="azure" size={22} place={{ top: '17%', left: '9%' }} />
      <Dot color="red" size={14} place={{ top: '58%', right: '11%' }} show="wide" />
      <Dot color="lime" size={28} place={{ bottom: '7%', left: '47%' }} />
      <Dot color="yellow" size={20} place={{ bottom: '11%', right: '8%' }} show="wide" />
      <div className={styles.heroText}>
        <p className={styles.eyebrow}>{copy.eyebrow}</p>
        <h1 id="home-hero-title" className={styles.h1}>{copy.title}</h1>
        <p className={styles.heroLead}>{copy.lead}</p>
        <EarlyAccessLink label={copy.cta} />
      </div>
    </section>
  );
}
