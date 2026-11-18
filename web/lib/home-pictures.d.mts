export type HomePictureName = 'driver' | 'dancer' | 'home' | 'stage' | 'meadow';
export type PictureFormat = 'avif' | 'webp';

export interface HomePicture {
  source: string;
  width: number;
  height: number;
  widths: number[];
}

export declare const HOME_PICTURES: Record<HomePictureName, HomePicture>;
export declare const PICTURE_FORMATS: PictureFormat[];
export declare function pictureUrl(name: HomePictureName, width: number, format: PictureFormat): string;
