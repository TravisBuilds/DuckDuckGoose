import { HOME_COPY } from '@/lib/home-copy.mjs';
import { Dot, FramedPicture } from './parts';
import styles from './home.module.css';

/** Slide 18: Be the star of your own story. */
export default function HowItWorks() {
  const copy = HOME_COPY.how;
  return (
    <section aria-labelledby="home-how-title" className={styles.section}>
      <Dot color="azure" size={20} place={{ top: '8%', right: '5%' }} show="wide" />
      <Dot color="red" size={12} place={{ top: '55%', left: '3%' }} show="wide" />
      <Dot color="lime" size={40} place={{ top: '44%', right: '4%' }} show="wide" />
      <Dot color="yellow" size={20} place={{ top: '20%', left: '52%' }} show="wide" />
      <Dot color="azure" size={18} place={{ top: 32, right: '10%' }} show="narrow" />
      <Dot color="red" size={10} place={{ top: 40, right: '22%' }} show="narrow" />
      <div className={`${styles.container} ${styles.howGrid}`}>
        <div className={styles.howText}>
          <p className={styles.eyebrow}>{copy.eyebrow}</p>
          <h2 id="home-how-title" className={styles.h2}>{copy.title}</h2>
          <p className={styles.body}>{copy.body}</p>
        </div>
        <div className={styles.howArt}>
          <FramedPicture shape="round" name="stage" alt={copy.alt} sizes="(min-width: 960px) 600px, 90vw" className={styles.roundFrame} />
        </div>
        <ol className={`${styles.card} ${styles.steps}`}>
          {copy.steps.map((step) => (
            <li key={step.title}>
              <h3 className={styles.cardTitle}>{step.title}</h3>
              <p className={styles.cardText}>{step.text}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
