"use client";
import Image from "next/image";
import { useState } from "react";

export default function MediaSlot({src,alt,ratio="16/9",className=""}:{src?:string;alt:string;ratio?:string;className?:string}){const[missing,setMissing]=useState(false);return <div className={`media-slot ${className}`} style={{aspectRatio:ratio}}>{src&&!missing?<Image src={src} alt={alt} fill unoptimized sizes="(max-width: 600px) 100vw, 50vw" onError={()=>setMissing(true)} style={{objectFit:"cover",objectPosition:"center 30%",borderRadius:6}}/>:<div role="img" aria-label={alt} className="media-fallback"/>}</div>}
