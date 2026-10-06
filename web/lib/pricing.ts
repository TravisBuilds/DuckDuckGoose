export interface PricingTier {
  name: string;
  price: number;
  credits: number;
  features: string[];
  recommended?: boolean;
}

export const pricingTiers: PricingTier[] = [
  {
    name: "Starter",
    price: 15,
    credits: 1500,
    features: [
      "1,500 credits per month",
      "2-4 short videos",
      "Standard quality",
      "Community support",
      "Basic templates"
    ]
  },
  {
    name: "Creator",
    price: 35,
    credits: 3900,
    recommended: true,
    features: [
      "3,900 credits per month",
      "7-11 videos",
      "Standard + Cinematic routes",
      "Priority support",
      "All templates",
      "Advanced customization"
    ]
  },
  {
    name: "Studio",
    price: 99,
    credits: 11800,
    features: [
      "11,800 credits per month",
      "23-34 videos",
      "Premium cinematic quality",
      "Dedicated support",
      "All templates",
      "API access (coming soon)",
      "Team collaboration (coming soon)"
    ]
  }
];

export const creditRate = 0.01; // 1 credit = $0.01
