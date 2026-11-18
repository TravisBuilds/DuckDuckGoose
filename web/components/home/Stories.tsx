import { HOME_COPY } from '@/lib/home-copy.mjs';
import { Dot, Picture } from './parts';
import styles from './home.module.css';

/** Slide 11: It's already happening, with the driver's and the dancer's stories. */
export default function Stories() {
  const copy = HOME_COPY.stories;
  return (
    <section aria-labelledby="home-stories-title" className={styles.section}>
      <Dot color="red" size={12} place={{ top: '9%', left: '62%' }} show="wide" />
      <Dot color="azure" size={20} place={{ top: '14%', right: '6%' }} show="wide" />
      <Dot color="lime" size={40} place={{ bottom: '9%', left: '34%' }} show="wide" />
      <Dot color="yellow" size={22} place={{ bottom: '6%', left: '4%' }} show="wide" />
      <Dot color="azure" size={18} place={{ top: 34, right: '12%' }} show="narrow" />
      <Dot color="yellow" size={14} place={{ bottom: 30, left: '16%' }} show="narrow" />
      <div className={`${styles.container} ${styles.storiesGrid}`}>
        <div className={styles.storiesText}>
          <p className={styles.eyebrow}>{copy.eyebrow}</p>
          <h2 id="home-stories-title" className={styles.h2}>{copy.title}</h2>
          <p className={styles.lead}>{copy.lead}</p>
          <p className={styles.body}>{copy.body}</p>
        </div>
        <ul className={styles.storyList}>
          {copy.items.map((story) => (
            <li key={story.key} className={styles.story}>
              <div className={styles.storyPhoto}>
                <Picture name={story.key} alt={story.alt} sizes="(min-width: 960px) 300px, (min-width: 640px) 40vw, 80vw" />
              </div>
              <div className={`${styles.card} ${styles.storyCard}`}>
                <h3 className={styles.cardTitle}>{story.title}</h3>
                <p className={styles.cardText}>{story.text}</p>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
