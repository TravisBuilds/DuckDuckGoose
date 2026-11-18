import { HOME_COPY } from '@/lib/home-copy.mjs';
import { Blob, Dot, EarlyAccessLink, FramedPicture } from './parts';
import styles from './home.module.css';

/** Slide 41: From video to everything, and the page's closing call to action. */
export default function Vision() {
  const copy = HOME_COPY.vision;
  return (
    <section aria-labelledby="home-vision-title" className={`${styles.section} ${styles.vision}`}>
      <Blob shape="wave" className={styles.visionPanel} show="wide" />
      <Dot color="yellow" size={20} place={{ top: '12%', left: '3%' }} show="wide" />
      <Dot color="azure" size={20} place={{ top: '46%', left: '2%' }} show="wide" />
      <Dot color="red" size={12} place={{ top: '7%', left: '45%' }} show="wide" />
      <Dot color="lime" size={40} place={{ bottom: '9%', left: '27%' }} show="wide" />
      <Dot color="yellow" size={18} place={{ top: 34, right: '12%' }} show="narrow" />
      <Dot color="lime" size={24} place={{ bottom: 28, right: '18%' }} show="narrow" />
      <div className={`${styles.container} ${styles.visionGrid}`}>
        <div className={styles.visionText}>
          <p className={styles.eyebrow}>{copy.eyebrow}</p>
          <h2 id="home-vision-title" className={styles.h2}>{copy.title}</h2>
          <p className={styles.body}>{copy.body}</p>
          <p className={styles.promise}>{copy.promise}</p>
        </div>
        <div className={styles.visionArt}>
          <Blob shape="wave" className={styles.visionArtWave} show="narrow" />
          <FramedPicture shape="round" name="meadow" alt={copy.alt} sizes="(min-width: 960px) 600px, 90vw" className={styles.roundFrame} />
          <div className={`${styles.card} ${styles.missionCard}`}>
            <p className={styles.mission}>{copy.mission}</p>
            <p className={styles.signature}>{copy.signature}</p>
          </div>
        </div>
        <EarlyAccessLink label={copy.cta} className={styles.visionCta} />
      </div>
    </section>
  );
}
