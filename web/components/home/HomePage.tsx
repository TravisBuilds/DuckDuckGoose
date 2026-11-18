import HomeFooter from './HomeFooter';
import HomeHeader from './HomeHeader';
import Hero from './Hero';
import HowItWorks from './HowItWorks';
import StartWithVideo from './StartWithVideo';
import Stories from './Stories';
import Vision from './Vision';
import { ClipPaths } from './parts';
import styles from './home.module.css';

/** The public home page, in the direction deck's order. Every word comes from lib/home-copy.mjs. */
export default function HomePage() {
  return (
    <div className={styles.page}>
      <ClipPaths />
      <HomeHeader />
      <main id="main" tabIndex={-1}>
        <Hero />
        <Stories />
        <StartWithVideo />
        <HowItWorks />
        <Vision />
      </main>
      <HomeFooter />
    </div>
  );
}
