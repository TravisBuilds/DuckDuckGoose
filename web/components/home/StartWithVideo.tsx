import { HOME_COPY } from '@/lib/home-copy.mjs';
import { Blob, Dot, FramedPicture } from './parts';
import styles from './home.module.css';

/** Slide 2: We start with video. */
export default function StartWithVideo() {
  const copy = HOME_COPY.video;
  return (
    <section aria-labelledby="home-video-title" className={`${styles.section} ${styles.video}`}>
      <Blob shape="bulge" className={styles.videoPanel} show="wide" />
      <Dot color="azure" size={20} place={{ top: '9%', left: '4%' }} show="wide" />
      <Dot color="red" size={14} place={{ top: '8%', right: '7%' }} show="wide" />
      <Dot color="lime" size={34} place={{ bottom: '8%', left: '33%' }} show="wide" />
      <Dot color="yellow" size={18} place={{ bottom: '7%', left: '4%' }} show="wide" />
      <Dot color="lime" size={22} place={{ top: 30, right: '14%' }} show="narrow" />
      <div className={`${styles.container} ${styles.videoGrid}`}>
        <div className={styles.videoText}>
          <p className={styles.eyebrow}>{copy.eyebrow}</p>
          <h2 id="home-video-title" className={styles.h2}>{copy.title}</h2>
          <p className={styles.lead}>{copy.lead}</p>
          <p className={styles.body}>{copy.body}</p>
          <p className={styles.closer}>{copy.closer}</p>
        </div>
        <div className={styles.videoArt}>
          <Blob shape="pebble" className={styles.videoArtBlob} show="narrow" />
          <FramedPicture shape="tall" name="home" alt={copy.alt} sizes="(min-width: 960px) 440px, 90vw" className={styles.tallFrame} />
          <div className={`${styles.card} ${styles.videoCard}`}>
            <p className={styles.cardTitle}>{copy.card.title}</p>
            <p className={styles.cardText}>{copy.card.text}</p>
          </div>
        </div>
      </div>
    </section>
  );
}
